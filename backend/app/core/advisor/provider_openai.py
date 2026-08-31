"""Any OpenAI-compatible chat endpoint.

This backend has to work against a moving target: OpenAI itself, OpenRouter,
Groq, DeepSeek, Mistral, and local servers like Ollama, LM Studio, vLLM and
LocalAI all speak "the OpenAI API" while disagreeing about the details.  Rather
than maintaining a per-vendor table that goes stale, the provider **negotiates
at runtime**: it tries the strictest option first and steps down whenever the
endpoint rejects something, remembering what worked so the cost is paid once.

Two ladders run independently:

* *structured output* - ``json_schema`` -> ``json_object`` -> prompt-only
* *request parameters* - individual knobs are dropped as the endpoint complains
  about them (``max_tokens`` vs ``max_completion_tokens``, fixed temperature,
  no system role)

Plain ``httpx`` is used rather than the ``openai`` package: the requests are two
JSON bodies, the header control matters for self-hosted endpoints, and it keeps
one more large dependency out of the image.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

from ...config import AdvisorSettings
from .base import (
    ADVICE_SCHEMA,
    AdviceProvider,
    AdvisorUnavailable,
    RawResponse,
    extract_json,
)

log = logging.getLogger(__name__)

try:
    import httpx
    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - httpx is a hard dependency in practice
    httpx = None  # type: ignore[assignment]
    SDK_AVAILABLE = False


SCHEMA_NAME = "optimizarr_advice"

# Appended when the endpoint cannot enforce a schema itself.
SCHEMA_INSTRUCTION = (
    "\n\nAntworte ausschliesslich mit einem JSON-Objekt nach genau diesem Schema "
    "(keine Markdown-Umrandung, kein Text davor oder danach):\n"
    + json.dumps(ADVICE_SCHEMA, ensure_ascii=False, indent=2)
)


def normalise_base_url(raw: str) -> str:
    """Turn whatever the user pasted into a usable API root.

    ``http://nas:11434`` becomes ``http://nas:11434/v1``, while paths that are
    already versioned (``https://openrouter.ai/api/v1``) are left alone - some
    services put the version somewhere other than the first path segment.
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = f"http://{url}"
    parts = urlparse(url)
    path = parts.path.rstrip("/")
    # Only add /v1 when there is no path at all; anything else is intentional.
    if not path:
        path = "/v1"
    return urlunparse((parts.scheme, parts.netloc, path, "", "", ""))


#: The ladder, strictest first.  ``json_schema_strict`` is real constrained
#: decoding where it works; ``json_schema_loose`` covers endpoints that accept
#: the field but not the guarantee; the last two need the schema in the prompt.
STRUCTURED_LADDER = ["json_schema_strict", "json_schema_loose", "json_object", "prompt"]

STRUCTURED_LABELS = {
    "json_schema_strict": "erzwungenes JSON-Schema",
    "json_schema_loose": "JSON-Schema ohne Garantie",
    "json_object": "JSON-Modus",
    "prompt": "Prompt-Anweisung",
}

#: What the settings dropdown offers, mapped onto ladder entries.
STRUCTURED_ALIASES = {
    "json_schema": "json_schema_strict",
    "json_object": "json_object",
    "prompt": "prompt",
}


class _Capabilities:
    """What this endpoint turned out to accept.  Learned, then reused."""

    def __init__(self, preferred_structured: str = "auto"):
        self.structured: str = STRUCTURED_ALIASES.get(
            preferred_structured, preferred_structured
        )
        self.token_field: str = "max_tokens"
        self.send_temperature: bool = True
        self.send_system_role: bool = True
        self.probed: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "structured": self.structured,
            "structured_label": STRUCTURED_LABELS.get(self.structured, self.structured),
            "token_field": self.token_field,
            "send_temperature": self.send_temperature,
            "send_system_role": self.send_system_role,
        }


# Error fragments -> which knob to give up on.  Matched case-insensitively
# against the endpoint's error body.
_PARAM_FIXES: list[tuple[tuple[str, ...], str]] = [
    (("max_tokens", "max_completion_tokens"), "token_field"),
    (("temperature", "does not support", "unsupported_value"), "temperature"),
    (("system", "role", "not supported"), "system_role"),
]

_SCHEMA_FAILURE_HINTS = (
    "response_format",
    "json_schema",
    "invalid_type",
    "unsupported",
    "not supported",
    "unrecognized",
    "unknown field",
    "extra fields",
    "invalid schema",
)


class OpenAICompatibleProvider(AdviceProvider):
    name = "openai_compatible"
    label = "OpenAI-kompatibler Endpunkt"

    def __init__(self, settings: AdvisorSettings):
        self.settings = settings
        self._caps = _Capabilities(settings.openai_structured_mode)
        self._client: Any = None

    # -- lifecycle ---------------------------------------------------------- #

    def is_configured(self) -> tuple[bool, str]:
        if not SDK_AVAILABLE:
            return False, "Das Python-Paket 'httpx' fehlt im Container."
        if not self.settings.openai_base_url.strip():
            return False, "Keine Endpunkt-URL hinterlegt."
        if not self.settings.openai_model.strip():
            return False, "Kein Modellname hinterlegt."
        return True, ""

    def describe_model(self) -> str:
        return self.settings.openai_model

    @property
    def base_url(self) -> str:
        return normalise_base_url(self.settings.openai_base_url)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self.settings.openai_api_key.strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        # Harmless for endpoints that ignore them, useful on OpenRouter.
        headers["HTTP-Referer"] = "https://github.com/gottschalkfelix4-source/optimizarr"
        headers["X-Title"] = "Optimizarr"
        return headers

    def _http(self) -> Any:
        ready, reason = self.is_configured()
        if not ready:
            raise AdvisorUnavailable(reason)
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(float(self.settings.timeout_seconds), connect=10.0),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover
                pass

    # -- request building --------------------------------------------------- #

    def _build_body(self, system: str, user: str, structured: str) -> dict[str, Any]:
        caps = self._caps
        # Only real constrained decoding makes the prompt copy redundant; every
        # weaker mode needs the schema spelled out, and several endpoints accept
        # the field while quietly ignoring it, so keeping it costs little.
        system_text = system if structured == "json_schema_strict" else system + SCHEMA_INSTRUCTION

        messages: list[dict[str, str]] = []
        if caps.send_system_role and self.settings.openai_send_system_role:
            messages.append({"role": "system", "content": system_text})
            messages.append({"role": "user", "content": user})
        else:
            # Endpoints without a system role get everything in the user turn.
            messages.append({"role": "user", "content": f"{system_text}\n\n{user}"})

        body: dict[str, Any] = {
            "model": self.settings.openai_model.strip(),
            "messages": messages,
            caps.token_field: int(self.settings.openai_max_tokens),
        }
        if caps.send_temperature:
            body["temperature"] = float(self.settings.openai_temperature)

        if structured in ("json_schema_strict", "json_schema_loose"):
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    # vLLM requires `name`; everyone else tolerates it.
                    "name": SCHEMA_NAME,
                    "schema": ADVICE_SCHEMA,
                    "strict": structured == "json_schema_strict",
                },
            }
        elif structured == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    def _apply_param_fix(self, error_text: str) -> bool:
        """Drop one request parameter the endpoint complained about.

        Returns True when something was changed and the request is worth
        retrying.
        """
        lowered = error_text.lower()
        caps = self._caps

        if "max_completion_tokens" in lowered and caps.token_field == "max_tokens":
            caps.token_field = "max_completion_tokens"
            log.info("endpoint wants max_completion_tokens - switched")
            return True
        if "max_tokens" in lowered and caps.token_field == "max_completion_tokens":
            caps.token_field = "max_tokens"
            return True
        if "temperature" in lowered and caps.send_temperature:
            caps.send_temperature = False
            log.info("endpoint rejects temperature - dropped")
            return True
        if (
            ("system" in lowered and ("role" in lowered or "not supported" in lowered))
            and caps.send_system_role
        ):
            caps.send_system_role = False
            log.info("endpoint rejects the system role - folding it into the user turn")
            return True
        return False

    @staticmethod
    def _looks_like_schema_problem(error_text: str) -> bool:
        lowered = error_text.lower()
        return any(hint in lowered for hint in _SCHEMA_FAILURE_HINTS)

    # -- the call ----------------------------------------------------------- #

    async def _post(self, body: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str]:
        client = self._http()
        url = f"{self.base_url}/chat/completions"
        try:
            response = await client.post(url, headers=self._headers(), json=body)
        except httpx.ConnectError as exc:
            raise AdvisorUnavailable(
                f"Endpunkt nicht erreichbar ({self.base_url}). "
                f"Laeuft der Dienst und ist er vom Container aus erreichbar? [{exc}]"
            ) from exc
        except httpx.TimeoutException as exc:
            raise AdvisorUnavailable(f"Zeitueberschreitung beim Endpunkt {self.base_url}.") from exc

        text = response.text
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            payload = None
        return response.status_code, payload, text

    async def complete(self, system: str, user: str, timeout: float) -> RawResponse:
        """One advice request, stepping down through the ladders as needed.

        A step counts as successful only when the reply actually parses as JSON.
        HTTP 200 on its own proves nothing: several endpoints accept
        ``response_format`` and then ignore it, which would otherwise leave us
        believing in a guarantee we never got.
        """
        caps = self._caps
        chosen = self.settings.openai_structured_mode
        if chosen != "auto":
            attempts = [STRUCTURED_ALIASES.get(chosen, chosen)]
        elif caps.probed:
            # Keep what worked, but leave room to fall further if it stops working.
            index = STRUCTURED_LADDER.index(caps.structured) if caps.structured in STRUCTURED_LADDER else 0
            attempts = STRUCTURED_LADDER[index:]
        else:
            attempts = list(STRUCTURED_LADDER)

        last_error = ""
        for position, structured in enumerate(attempts):
            is_last = position == len(attempts) - 1
            # Up to three tries per mode: each parameter fix earns one retry.
            for _ in range(3):
                body = self._build_body(system, user, structured)
                status, payload, text = await self._post(body)

                if status == 200 and payload is not None:
                    raw = self._parse_success(payload)
                    if raw.refused:
                        return raw
                    if raw.truncated:
                        raise AdvisorUnavailable(
                            "Die Antwort wurde abgeschnitten - unter 'Feineinstellungen' "
                            "die maximale Antwortlaenge erhoehen."
                        )
                    if extract_json(raw.text) is not None:
                        caps.structured = structured
                        caps.probed = True
                        return raw
                    # Answered, but not with usable JSON: a weaker mode puts the
                    # schema in the prompt, which often fixes exactly this.
                    last_error = "Antwort war kein verwertbares JSON"
                    log.info("endpoint returned unusable output in %s mode", structured)
                    break

                error_text = _error_message(payload, text)
                last_error = f"HTTP {status}: {error_text[:300]}"

                if status in (401, 403):
                    raise AdvisorUnavailable(
                        "Zugang abgelehnt - API-Key pruefen "
                        f"(HTTP {status}: {error_text[:160]})"
                    )
                if status == 404:
                    raise AdvisorUnavailable(
                        f"Endpunkt oder Modell nicht gefunden: {self.base_url}/chat/completions "
                        f"mit Modell '{self.settings.openai_model}'."
                    )
                if status == 429:
                    raise AdvisorUnavailable("Rate-Limit des Endpunkts erreicht (429).")
                if status >= 500:
                    raise AdvisorUnavailable(
                        f"Der Endpunkt meldet einen Serverfehler (HTTP {status})."
                    )
                if _is_our_schema_fault(error_text):
                    # Our schema is wrong, not their support - stepping down
                    # would hide a bug we should see.
                    raise AdvisorUnavailable(
                        f"Der Endpunkt lehnt das Anfrage-Schema ab: {error_text[:200]}"
                    )

                # 400/422: either a parameter the endpoint dislikes, or the schema.
                if self._apply_param_fix(error_text):
                    continue
                break  # move on to the next structured mode

            if is_last:
                break

        raise AdvisorUnavailable(f"Anfrage abgelehnt. {last_error}")

    @staticmethod
    def _parse_success(payload: dict[str, Any]) -> RawResponse:
        raw = RawResponse(model=str(payload.get("model") or ""))
        usage = payload.get("usage") or {}
        raw.input_tokens = int(usage.get("prompt_tokens") or 0)
        raw.output_tokens = int(usage.get("completion_tokens") or 0)

        choices = payload.get("choices") or []
        if not choices:
            return raw
        first = choices[0] or {}
        message = first.get("message") or {}
        finish = first.get("finish_reason")

        # A refusal has to be seen before the content is read - when it is set,
        # content is empty and parsing it would report the wrong problem.
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            raw.refused = True
            raw.text = refusal
            return raw
        if finish == "content_filter":
            raw.refused = True
            return raw
        if finish == "length":
            # Truncated JSON is a token-budget problem, not a parse problem.
            raw.truncated = True

        content = message.get("content")
        if isinstance(content, list):
            # Some servers return the multimodal content-part shape.
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in ("text", "output_text")
            )
        raw.text = content or ""

        if not raw.text:
            # Reasoning models sometimes leave content empty and put the answer
            # in a tool call or a reasoning field.
            for key in ("reasoning_content", "reasoning"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    raw.text = value
                    break
            tool_calls = message.get("tool_calls") or []
            if not raw.text and tool_calls:
                args = (tool_calls[0].get("function") or {}).get("arguments")
                if isinstance(args, str):
                    raw.text = args

        return raw

    # -- diagnostics -------------------------------------------------------- #

    async def check(self) -> tuple[bool, str]:
        ready, reason = self.is_configured()
        if not ready:
            return False, reason

        notes: list[str] = []
        client = self._http()

        # 1. Is anything listening, and does it know the model?
        try:
            models_response = await client.get(f"{self.base_url}/models", headers=self._headers())
            if models_response.status_code == 200:
                data = models_response.json()
                ids = [
                    str(m.get("id"))
                    for m in (data.get("data") or [])
                    if isinstance(m, dict) and m.get("id")
                ]
                wanted = self.settings.openai_model.strip()
                if ids and wanted not in ids:
                    close = [m for m in ids if wanted.lower() in m.lower()][:3]
                    notes.append(
                        f"Modell '{wanted}' steht nicht in der Liste des Endpunkts"
                        + (f" - gefunden: {', '.join(close)}" if close else
                           f" ({len(ids)} Modelle verfuegbar)")
                    )
                elif ids:
                    notes.append(f"Modell gefunden ({len(ids)} verfuegbar)")
            elif models_response.status_code in (401, 403):
                return False, f"Zugang abgelehnt (HTTP {models_response.status_code}) - API-Key pruefen."
        except Exception as exc:
            notes.append(f"Modell-Liste nicht abrufbar ({type(exc).__name__})")

        # 2. The only test that proves anything: a real request.
        try:
            raw = await self.complete(
                "Antworte ausschliesslich mit gueltigem JSON.",
                'Gib exakt dieses JSON zurueck: {"ok": true}',
                float(self.settings.timeout_seconds),
            )
        except AdvisorUnavailable as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {str(exc)[:200]}"

        mode = {
            "json_schema": "mit erzwungenem JSON-Schema",
            "json_object": "im JSON-Modus",
            "prompt": "ueber Prompt-Anweisung",
        }.get(self._caps.structured, self._caps.structured)

        detail = f"Verbindung erfolgreich ({raw.model or self.settings.openai_model}, {mode})"
        if notes:
            detail += ". " + "; ".join(notes)
        if self._caps.token_field != "max_tokens" or not self._caps.send_temperature:
            detail += " [angepasst: " + ", ".join(
                filter(None, [
                    self._caps.token_field if self._caps.token_field != "max_tokens" else "",
                    "ohne temperature" if not self._caps.send_temperature else "",
                    "ohne system-Rolle" if not self._caps.send_system_role else "",
                ])
            ) + "]"
        return True, detail

    def capabilities(self) -> dict[str, Any]:
        return self._caps.snapshot()


#: Errors that mean OUR schema is malformed.  Stepping down the ladder would
#: only hide the bug, so these are raised instead.
_OUR_FAULT_HINTS = (
    "invalid schema",
    "required fields are missing",
    "is not permitted",
    "additionalproperties",
)


def _is_our_schema_fault(error_text: str) -> bool:
    lowered = (error_text or "").lower()
    return any(hint in lowered for hint in _OUR_FAULT_HINTS)


def _error_message(payload: dict[str, Any] | None, fallback: str) -> str:
    """Pull a human-readable message out of the many error shapes in the wild."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "type", "code"):
                value = error.get(key)
                if isinstance(value, str) and value:
                    return value
        if isinstance(error, str) and error:
            return error
        for key in ("message", "detail", "msg"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return (fallback or "").strip()
