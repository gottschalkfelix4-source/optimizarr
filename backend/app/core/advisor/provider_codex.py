"""Talk to the ChatGPT backend with credentials from the browser sign-in.

This is the same path the Codex CLI takes: a ChatGPT account token against the
Responses API on ``chatgpt.com/backend-api``, rather than a platform API key
against ``api.openai.com``.  The request shape is the Responses API, and the
endpoint streams its answer as server-sent events, so the text has to be
reassembled from deltas.

Tokens are stored in the ``oauth_credentials`` table and refreshed here, lazily,
whenever they are close to expiring - the encoder can run for hours between
advisor calls, so "refresh on use" is the only scheme that holds up.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import platform
import uuid
from typing import Any

from ...config import AdvisorSettings
from ...db import session_scope
from ...models import OAuthCredential, utcnow
from . import codex_oauth
from .base import ADVICE_SCHEMA, AdviceProvider, AdvisorUnavailable, RawResponse
from .codex_oauth import OAuthError, TokenSet

log = logging.getLogger(__name__)

try:
    import httpx
    HTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    HTTP_AVAILABLE = False

PROVIDER_KEY = "openai_codex"

BASE_URL = "https://chatgpt.com/backend-api/codex"
RESPONSES_URL = f"{BASE_URL}/responses"
MODELS_URL = f"{BASE_URL}/models"

#: The backend gates on the originator, and a value outside its allow-list is a
#: documented cause of 403s.  The user agent is built to match the CLI's shape
#: for the same reason.
ORIGINATOR = codex_oauth.ORIGINATOR
CLIENT_VERSION = "0.144.1"

#: Fallback list only.  Model slugs rotate and depend on the account's plan, so
#: the real list is fetched from the backend and cached; this is what the UI
#: offers when that call fails.
FALLBACK_MODELS = [
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.1-codex",
    "gpt-5.1",
    "gpt-5",
]
KNOWN_MODELS = FALLBACK_MODELS  # kept for callers that imported the old name

#: How long a fetched model list stays fresh.
MODEL_CACHE_TTL = dt.timedelta(hours=24)

SCHEMA_INSTRUCTION = (
    "\n\nAntworte ausschliesslich mit einem JSON-Objekt nach genau diesem Schema "
    "(keine Markdown-Umrandung, kein Text davor oder danach):\n"
    + json.dumps(ADVICE_SCHEMA, ensure_ascii=False, indent=2)
)


def user_agent() -> str:
    """Mirror the CLI's user-agent shape.

    Third-party clients that send an obviously different agent have been seen
    getting 403s from this endpoint, so there is no upside in being creative
    here.  The originator is the part that is actually checked; the rest just
    keeps the shape plausible.
    """
    system = platform.system() or "Unknown"
    release = platform.release() or "0"
    machine = platform.machine() or "unknown"
    return f"{ORIGINATOR}/{CLIENT_VERSION} ({system} {release}; {machine}) unknown"


# --------------------------------------------------------------------------- #
# Credential storage
# --------------------------------------------------------------------------- #

def load_tokens() -> TokenSet | None:
    """Read the stored ChatGPT credentials, if there are any.

    Swallows database errors on purpose: a missing table on a not-yet-migrated
    install must read as "not signed in", never as a crash that takes the
    advisor - and with it the scan - down with it.
    """
    try:
        with session_scope() as s:
            row = s.get(OAuthCredential, PROVIDER_KEY)
            if row is None or not (row.access_token or row.refresh_token):
                return None
            return TokenSet(
                access_token=row.access_token,
                refresh_token=row.refresh_token,
                id_token=row.id_token,
                account_id=row.account_id,
                account_label=row.account_label,
                plan_type=row.plan_type,
                expires_at=row.expires_at,
                # Re-read on load: some headers depend on claims we do not
                # denormalise into columns.
                raw_claims=codex_oauth.decode_jwt_claims(row.id_token).get(
                    codex_oauth.AUTH_CLAIM, {}
                ) or {},
            )
    except Exception:
        log.debug("could not read stored ChatGPT credentials", exc_info=True)
        return None


def store_tokens(tokens: TokenSet, error: str = "") -> None:
    with session_scope() as s:
        row = s.get(OAuthCredential, PROVIDER_KEY)
        if row is None:
            row = OAuthCredential(provider=PROVIDER_KEY)
            s.add(row)
        row.access_token = tokens.access_token
        row.refresh_token = tokens.refresh_token
        row.id_token = tokens.id_token
        row.account_id = tokens.account_id
        row.account_label = tokens.account_label
        row.plan_type = tokens.plan_type
        row.expires_at = tokens.expires_at
        row.last_error = error
        if not error:
            row.last_refresh = utcnow()


def clear_tokens() -> None:
    with session_scope() as s:
        row = s.get(OAuthCredential, PROVIDER_KEY)
        if row is not None:
            s.delete(row)


def credential_status() -> dict[str, Any]:
    """What the settings screen shows about the current sign-in."""
    try:
        return _credential_status()
    except Exception:
        log.debug("could not read credential status", exc_info=True)
        return {"signed_in": False}


def _credential_status() -> dict[str, Any]:
    with session_scope() as s:
        row = s.get(OAuthCredential, PROVIDER_KEY)
        if row is None:
            return {"signed_in": False}
        expires = row.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.timezone.utc)
        return {
            "signed_in": bool(row.access_token or row.refresh_token),
            "account_label": row.account_label,
            "plan_type": row.plan_type,
            "account_id_present": bool(row.account_id),
            "expires_at": expires.isoformat() if expires else None,
            "expired": bool(expires and dt.datetime.now(dt.timezone.utc) >= expires),
            "can_refresh": bool(row.refresh_token),
            "last_refresh": row.last_refresh.isoformat() if row.last_refresh else None,
            "last_error": row.last_error,
        }


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #

class CodexProvider(AdviceProvider):
    name = "openai_codex"
    label = "ChatGPT-Anmeldung (Codex)"

    def __init__(self, settings: AdvisorSettings):
        self.settings = settings
        self._client: Any = None
        self._refresh_lock = asyncio.Lock()
        self._session_id = str(uuid.uuid4())
        #: None = not tried yet, False = backend rejected it, True = accepted.
        self._schema_supported: bool | None = None
        self._models_cache: list[str] = []
        self._models_fetched_at: dt.datetime | None = None

    # -- lifecycle ---------------------------------------------------------- #

    def is_configured(self) -> tuple[bool, str]:
        if not HTTP_AVAILABLE:
            return False, "Das Python-Paket 'httpx' fehlt im Container."
        tokens = load_tokens()
        if tokens is None:
            return False, "Nicht mit ChatGPT angemeldet."
        if not tokens.access_token and not tokens.refresh_token:
            return False, "Keine gueltigen Anmeldedaten hinterlegt."
        if tokens.is_expired and not tokens.refresh_token:
            return False, "Die Anmeldung ist abgelaufen - bitte erneut anmelden."
        return True, ""

    def describe_model(self) -> str:
        return self.settings.codex_model

    def _http(self) -> Any:
        if not HTTP_AVAILABLE:
            raise AdvisorUnavailable("Das Python-Paket 'httpx' fehlt im Container.")
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(float(self.settings.timeout_seconds), connect=15.0),
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

    # -- tokens ------------------------------------------------------------- #

    async def _valid_tokens(self) -> TokenSet:
        """Return usable tokens, refreshing them if they are about to expire."""
        tokens = load_tokens()
        if tokens is None:
            raise AdvisorUnavailable("Nicht mit ChatGPT angemeldet.")
        if not tokens.is_expired and tokens.access_token:
            return tokens

        async with self._refresh_lock:
            # Another task may have refreshed while we waited for the lock.
            tokens = load_tokens()
            if tokens is None:
                raise AdvisorUnavailable("Nicht mit ChatGPT angemeldet.")
            if not tokens.is_expired and tokens.access_token:
                return tokens
            if not tokens.refresh_token:
                raise AdvisorUnavailable(
                    "Die ChatGPT-Anmeldung ist abgelaufen - bitte erneut anmelden."
                )
            try:
                refreshed = await codex_oauth.refresh_tokens(tokens)
            except OAuthError as exc:
                await asyncio.to_thread(store_tokens, tokens, str(exc))
                raise AdvisorUnavailable(
                    f"Anmeldung konnte nicht erneuert werden: {exc}"
                ) from exc
            await asyncio.to_thread(store_tokens, refreshed)
            log.info("refreshed ChatGPT credentials")
            return refreshed

    def _headers(self, tokens: TokenSet) -> dict[str, str]:
        """Headers the ChatGPT backend expects.

        Two things that look harmless and are not: ``OpenAI-Beta`` is no longer
        part of this path (the CLI stopped sending it), and the session header
        is ``session-id`` with a hyphen - the underscore spelling was removed
        backend-side.
        """
        headers = {
            "Authorization": f"Bearer {tokens.access_token}",  # access, not id token
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "originator": ORIGINATOR,
            "User-Agent": user_agent(),
            "version": CLIENT_VERSION,
            "session-id": self._session_id,
            "thread-id": str(uuid.uuid4()),
        }
        if tokens.account_id:
            # HTTP headers are case-insensitive; the CLI sends PascalCase.
            headers["ChatGPT-Account-ID"] = tokens.account_id
        if tokens.raw_claims.get("chatgpt_account_is_fedramp"):
            headers["X-OpenAI-Fedramp"] = "true"
        return headers

    # -- requests ----------------------------------------------------------- #

    def _build_body(self, system: str, user: str, with_schema: bool = True) -> dict[str, Any]:
        """Responses API request.

        Three constraints are not negotiable on this endpoint: ``store`` must be
        false, ``stream`` must be true (there is no non-streaming mode), and
        ``include`` must carry ``reasoning.encrypted_content``.  ``max_output_tokens``
        is rejected outright, so it is absent.

        The schema goes in ``text.format`` **flat** - name, strict and schema sit
        directly inside it.  This is not the shape Chat Completions uses, and
        copying that nested shape here gets it either rejected or, worse,
        silently ignored.  The instructions repeat the schema anyway, so a
        backend that ignores the field still produces usable output.
        """
        body: dict[str, Any] = {
            "model": self.settings.codex_model.strip() or FALLBACK_MODELS[0],
            "instructions": system + SCHEMA_INSTRUCTION,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": user}],
                }
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "reasoning": {"effort": self.settings.codex_reasoning_effort, "summary": "auto"},
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        if with_schema:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "optimizarr_advice",
                    "strict": True,
                    "schema": ADVICE_SCHEMA,
                }
            }
        return body

    async def complete(self, system: str, user: str, timeout: float) -> RawResponse:
        """One advice request.

        Tries with the schema attached and drops it if the backend objects -
        this endpoint is undocumented and first-party, so its accepted fields
        move without notice.  The instructions carry the schema either way.
        """
        attempts = [True, False] if self._schema_supported is not False else [False]
        last_error: AdvisorUnavailable | None = None

        for with_schema in attempts:
            try:
                raw = await self._request(system, user, timeout, with_schema)
            except _SchemaRejected as exc:
                self._schema_supported = False
                last_error = AdvisorUnavailable(str(exc))
                log.info("ChatGPT backend rejected the response schema - retrying without it")
                continue
            if with_schema:
                self._schema_supported = True
            return raw

        raise last_error or AdvisorUnavailable("Anfrage an ChatGPT fehlgeschlagen.")

    async def _request(
        self, system: str, user: str, timeout: float, with_schema: bool
    ) -> RawResponse:
        tokens = await self._valid_tokens()
        client = self._http()
        body = self._build_body(system, user, with_schema=with_schema)
        raw = RawResponse(model=self.settings.codex_model)

        try:
            async with client.stream(
                "POST", RESPONSES_URL, headers=self._headers(tokens), json=body,
                timeout=httpx.Timeout(timeout, connect=15.0),
            ) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", "replace")
                    if with_schema and _looks_like_schema_rejection(response.status_code, detail):
                        raise _SchemaRejected(codex_oauth.redact(detail)[:200])
                    raise AdvisorUnavailable(
                        _explain_status(response.status_code, codex_oauth.redact(detail))
                    )
                raw.text, raw.input_tokens, raw.output_tokens = await _read_sse(response)
        except (AdvisorUnavailable, _SchemaRejected):
            raise
        except httpx.ConnectError as exc:
            raise AdvisorUnavailable(
                "Keine Verbindung zu chatgpt.com - hat der Container Internetzugang?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise AdvisorUnavailable("Zeitueberschreitung bei chatgpt.com.") from exc

        if not raw.text:
            raise AdvisorUnavailable("Die Antwort von ChatGPT enthielt keinen Text.")
        return raw

    async def list_models(self, force: bool = False) -> tuple[list[str], str]:
        """Model slugs this account can actually use.

        Slugs rotate and depend on the plan, so the backend is asked rather than
        guessed; the static list is only what the UI falls back to.
        """
        now = dt.datetime.now(dt.timezone.utc)
        if not force and self._models_cache and self._models_fetched_at:
            if now - self._models_fetched_at < MODEL_CACHE_TTL:
                return self._models_cache, "aus dem Zwischenspeicher"

        try:
            tokens = await self._valid_tokens()
            client = self._http()
            response = await client.get(
                MODELS_URL,
                headers={**self._headers(tokens), "Accept": "application/json"},
                params={"client_version": CLIENT_VERSION},
                timeout=httpx.Timeout(30.0, connect=15.0),
            )
        except (AdvisorUnavailable, Exception) as exc:  # noqa: B014 - deliberate catch-all
            log.debug("could not fetch Codex model list: %s", exc)
            return FALLBACK_MODELS, "Liste nicht abrufbar - eingebaute Auswahl"

        if response.status_code != 200:
            return FALLBACK_MODELS, (
                f"Liste nicht abrufbar (HTTP {response.status_code}) - eingebaute Auswahl"
            )
        try:
            payload = response.json()
        except ValueError:
            return FALLBACK_MODELS, "Antwort unlesbar - eingebaute Auswahl"

        slugs: list[str] = []
        entries = payload if isinstance(payload, list) else (
            payload.get("models") or payload.get("data") or []
        )
        for entry in entries:
            if isinstance(entry, dict):
                slug = entry.get("slug") or entry.get("id")
                if isinstance(slug, str) and slug:
                    slugs.append(slug)
            elif isinstance(entry, str):
                slugs.append(entry)

        if not slugs:
            return FALLBACK_MODELS, "Keine Modelle gemeldet - eingebaute Auswahl"
        self._models_cache = slugs
        self._models_fetched_at = now
        return slugs, f"{len(slugs)} Modelle vom Konto abgerufen"

    async def check(self) -> tuple[bool, str]:
        ready, reason = self.is_configured()
        if not ready:
            return False, reason
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

        status = credential_status()
        who = status.get("account_label") or "ChatGPT-Konto"
        plan = f", {status['plan_type']}" if status.get("plan_type") else ""
        return True, (
            f"Verbindung erfolgreich als {who}{plan} "
            f"({self.settings.codex_model}): {raw.text.strip()[:60]}"
        )


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #

async def _read_sse(response: Any) -> tuple[str, int, int]:
    """Reassemble the response text from the SSE stream.

    Handles both shapes seen in the wild: incremental ``output_text.delta``
    events, and a single ``response.completed`` carrying the finished object.
    """
    chunks: list[str] = []
    completed_text = ""
    input_tokens = output_tokens = 0

    async for line in response.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        kind = str(event.get("type") or "")

        if kind.endswith("output_text.delta"):
            delta = event.get("delta")
            if isinstance(delta, str):
                chunks.append(delta)
            elif isinstance(delta, dict) and isinstance(delta.get("text"), str):
                chunks.append(delta["text"])
        elif kind.endswith("output_text.done"):
            text = event.get("text")
            if isinstance(text, str) and text and not chunks:
                chunks.append(text)
        elif kind in ("response.completed", "response.incomplete"):
            payload = event.get("response") or {}
            completed_text = _text_from_response(payload) or completed_text
            usage = payload.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
        elif kind in ("response.failed", "error"):
            payload = event.get("response") or event
            error = payload.get("error") or {}
            message = ""
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("code") or "")
            raise AdvisorUnavailable(
                f"ChatGPT hat die Anfrage abgebrochen{': ' + message if message else '.'}"
            )

    text = "".join(chunks).strip() or completed_text.strip()
    return text, input_tokens, output_tokens


def _text_from_response(payload: dict[str, Any]) -> str:
    """Dig the assistant text out of a finished Responses-API object."""
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    if isinstance(direct, list):
        joined = "".join(p for p in direct if isinstance(p, str))
        if joined.strip():
            return joined

    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in (None, "message"):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
    return "".join(parts)


class _SchemaRejected(Exception):
    """The backend refused the response schema - retry without it."""


_SCHEMA_HINTS = (
    "text.format", "json_schema", "response_format", "invalid schema",
    "unrecognized", "unknown field", "not supported", "unexpected",
)


def _looks_like_schema_rejection(status: int, detail: str) -> bool:
    if status not in (400, 422):
        return False
    lowered = (detail or "").lower()
    return any(hint in lowered for hint in _SCHEMA_HINTS)


def _explain_status(status: int, detail: str) -> str:
    """Turn an HTTP failure into something a home-server user can act on."""
    snippet = (detail or "").strip()[:240]
    lowered = snippet.lower()

    if status == 401:
        return (
            "ChatGPT hat die Anmeldung abgelehnt (401). Die Sitzung ist vermutlich "
            "abgelaufen - bitte in den Einstellungen erneut anmelden."
        )
    if status == 403:
        return (
            "Zugriff verweigert (403). Bekannte Ursachen: das Konto hat keinen "
            "Codex-Zugang, die Region ist gesperrt, oder OpenAI laesst diesen Client "
            "nicht zu. In dem Fall bleibt ein OpenAI-kompatibler Endpunkt mit "
            f"API-Key. {snippet}"
        )
    if status == 404:
        if "model" in lowered:
            return (
                f"Modell nicht gefunden. Das heisst meist nicht, dass es das Modell nicht "
                "gibt - OpenAI gibt Modelle je nach Client und Version frei. Unter "
                "'Modell' die vom Konto gemeldete Liste abrufen und einen Eintrag daraus "
                "waehlen."
            )
        return (
            "Endpunkt nicht gefunden (404) - OpenAI hat die Schnittstelle vermutlich "
            "geaendert. Bis dahin hilft ein OpenAI-kompatibler Endpunkt mit API-Key."
        )
    if status == 429:
        return (
            "Kontingent erschoepft oder zu viele Anfragen (429). Das ChatGPT-Abo hat "
            "eigene Nutzungsgrenzen. Spaeter erneut versuchen."
        )
    if status >= 500:
        return f"ChatGPT meldet einen Serverfehler (HTTP {status})."
    return f"Anfrage abgelehnt (HTTP {status}): {snippet}"
