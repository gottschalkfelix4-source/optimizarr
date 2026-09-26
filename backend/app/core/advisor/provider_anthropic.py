"""Claude via the Anthropic API."""
from __future__ import annotations

import logging
from typing import Any

from ...config import AdvisorSettings
from .base import ADVICE_SCHEMA, AdviceProvider, AdvisorUnavailable, RawResponse

log = logging.getLogger(__name__)

try:
    import anthropic
    from anthropic import AsyncAnthropic
    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    anthropic = None  # type: ignore[assignment]
    AsyncAnthropic = None  # type: ignore[assignment]
    SDK_AVAILABLE = False


#: Models older than the 4.5 generation, where ``effort`` is rejected and
#: thinking only exists as a fixed ``budget_tokens`` budget.
_LEGACY_MARKERS = (
    "claude-3", "sonnet-4-5", "sonnet-4-0", "sonnet-4-2025", "opus-4-1", "opus-4-0",
    "opus-4-2025",
)


def model_features(model: str) -> tuple[bool, bool]:
    """(accepts ``output_config.effort``, accepts adaptive thinking).

    Claude Haiku 4.5 supports neither - a request carrying either one is
    rejected with a 400, so the advisor never answered with Haiku selected.
    Opus 4.5 knows effort but not adaptive thinking.  Every current model
    (Opus 5.5, Opus 5, Sonnet 5, Fable 5.1) and anything unknown - presumably
    newer - gets both.  Thinking is simply left out where it is not adaptive:
    the advisor's question is simple, a thinking budget would only cost.
    """
    name = (model or "").strip().lower()
    if "haiku" in name or any(marker in name for marker in _LEGACY_MARKERS):
        return False, False
    if "opus-4-5" in name:
        return True, False
    return True, True


class AnthropicProvider(AdviceProvider):
    name = "anthropic"
    label = "Claude (Anthropic API)"

    def __init__(self, settings: AdvisorSettings):
        self.settings = settings
        self._client: Any = None

    # -- lifecycle ---------------------------------------------------------- #

    def is_configured(self) -> tuple[bool, str]:
        if not SDK_AVAILABLE:
            return False, "Das Python-Paket 'anthropic' ist im Container nicht installiert."
        if not self.settings.api_key.strip():
            return False, "Kein Anthropic-API-Key hinterlegt."
        return True, ""

    def describe_model(self) -> str:
        return self.settings.model

    def _client_or_raise(self) -> Any:
        ready, reason = self.is_configured()
        if not ready:
            raise AdvisorUnavailable(reason)
        if self._client is None:
            self._client = AsyncAnthropic(
                api_key=self.settings.api_key.strip(),
                timeout=float(self.settings.timeout_seconds),
                max_retries=1,
            )
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.close()
            except Exception:  # pragma: no cover - best effort
                pass

    # -- requests ----------------------------------------------------------- #

    async def complete(self, system: str, user: str, timeout: float) -> RawResponse:
        client = self._client_or_raise()
        effort, adaptive = model_features(self.settings.model)
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": ADVICE_SCHEMA},
        }
        if effort:
            output_config["effort"] = "low"
        extra: dict[str, Any] = {}
        if adaptive:
            extra["thinking"] = {"type": "adaptive"}
        # No prompt caching: the system prompt is roughly 400 tokens, below the
        # minimum cacheable prefix of every current model (512 on Opus 5 and
        # Fable, 1024 on Sonnet 5, 4096 on Haiku 4.5).  A cache marker on it
        # would be silently ignored, so there is none to suggest otherwise.
        response = await client.messages.create(
            model=self.settings.model,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
            **extra,
        )

        raw = RawResponse(model=getattr(response, "model", self.settings.model))
        if getattr(response, "stop_reason", None) == "refusal":
            raw.refused = True
            return raw

        usage = getattr(response, "usage", None)
        if usage is not None:
            raw.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            raw.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                raw.text = block.text
                break
        return raw

    async def check(self) -> tuple[bool, str]:
        ready, reason = self.is_configured()
        if not ready:
            return False, reason
        try:
            client = self._client_or_raise()
            effort, _ = model_features(self.settings.model)
            response = await client.messages.create(
                model=self.settings.model,
                max_tokens=64,
                messages=[{"role": "user", "content": "Antworte nur mit: OK"}],
                **({"output_config": {"effort": "low"}} if effort else {}),
            )
        except Exception as exc:
            return False, friendly_error(exc)
        text = ""
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text = block.text.strip()
                break
        return True, f"Verbindung erfolgreich ({self.settings.model}): {text[:60]}"


def friendly_error(exc: Exception) -> str:
    """Turn SDK exceptions into something a UI can show."""
    if SDK_AVAILABLE and anthropic is not None:
        if isinstance(exc, anthropic.AuthenticationError):
            return "API-Key wurde abgelehnt (401). Bitte Key pruefen."
        if isinstance(exc, anthropic.PermissionDeniedError):
            return "Zugriff verweigert (403) - hat der Key Zugriff auf dieses Modell?"
        if isinstance(exc, anthropic.NotFoundError):
            return "Modell nicht gefunden - bitte ein anderes Modell waehlen."
        if isinstance(exc, anthropic.RateLimitError):
            return "Rate-Limit erreicht (429). Spaeter erneut versuchen."
        if isinstance(exc, anthropic.APITimeoutError):
            return "Zeitueberschreitung bei der Anfrage."
        if isinstance(exc, anthropic.APIConnectionError):
            return "Keine Verbindung zur Claude-API (Netzwerk/DNS im Container pruefen)."
        if isinstance(exc, anthropic.APIStatusError):
            return f"API-Fehler {exc.status_code}: {str(exc)[:200]}"
    return f"{type(exc).__name__}: {str(exc)[:200]}"
