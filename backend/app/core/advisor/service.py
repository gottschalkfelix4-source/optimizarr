"""The advisor itself: picks a backend, enforces the budget, sanitises the reply.

Design rules, in order of importance:

* **Never blocking.**  Any failure - no key, rate limit, timeout, refusal, bad
  JSON, a provider that went away - falls back to the local decision.  The
  advisor can only ever refine, never gate.
* **Never unbounded.**  CRF nudges are clamped to ``max_crf_delta`` and the
  number of calls per scan is capped, whichever backend is in use.
* **Filenames are data, not instructions.**  Anyone who can write a file into
  the library can put text in its name, so the system prompt says so and every
  value that comes back is range-checked (see ``base.sanitize``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...config import AdvisorSettings
from .base import (
    Advice,
    AdviceProvider,
    AdvisorUnavailable,
    SYSTEM_PROMPT,
    build_user_message,
    extract_json,
    sanitize,
)

log = logging.getLogger(__name__)


def build_provider(settings: AdvisorSettings) -> AdviceProvider:
    """Instantiate the configured backend."""
    from .provider_anthropic import AnthropicProvider
    from .provider_codex import CodexProvider
    from .provider_openai import OpenAICompatibleProvider

    if settings.provider == "openai_compatible":
        return OpenAICompatibleProvider(settings)
    if settings.provider == "openai_codex":
        return CodexProvider(settings)
    return AnthropicProvider(settings)


def provider_catalogue() -> list[dict[str, Any]]:
    """Metadata for the settings screen - which backends exist and what they need."""
    from .provider_anthropic import SDK_AVAILABLE as ANTHROPIC_SDK
    from .provider_openai import SDK_AVAILABLE as OPENAI_SDK

    return [
        {
            "id": "anthropic",
            "label": "Claude (Anthropic API)",
            "hint": "API-Key von console.anthropic.com. Abrechnung pro Anfrage.",
            "needs": ["api_key", "model"],
            "sdk_installed": ANTHROPIC_SDK,
        },
        {
            "id": "openai_codex",
            "label": "ChatGPT-Anmeldung (Codex)",
            "hint": (
                "Anmeldung im Browser mit dem ChatGPT-Konto, wie beim Codex-CLI. "
                "Nutzt das bestehende Abo statt API-Guthaben."
            ),
            "needs": ["oauth"],
            "sdk_installed": True,  # implemented with plain HTTP, no SDK needed
        },
        {
            "id": "openai_compatible",
            "label": "OpenAI-kompatibler Endpunkt",
            "hint": (
                "Beliebiger Dienst mit OpenAI-API: OpenAI selbst, OpenRouter, Groq, "
                "DeepSeek, oder lokal via Ollama, LM Studio, vLLM, LocalAI."
            ),
            "needs": ["openai_base_url", "openai_model", "openai_api_key"],
            "sdk_installed": OPENAI_SDK,
        },
    ]


class Advisor:
    """Wraps whichever provider is configured, with a per-scan call budget."""

    def __init__(self, settings: AdvisorSettings):
        self.settings = settings
        self.provider: AdviceProvider = build_provider(settings)
        self._calls_used = 0
        self._lock = asyncio.Lock()
        self.last_error = ""

    # -- state -------------------------------------------------------------- #

    @property
    def available(self) -> bool:
        if not self.settings.enabled:
            return False
        ready, _ = self.provider.is_configured()
        return ready

    def readiness(self) -> tuple[bool, str]:
        """(ready, reason) - used by the UI to explain why nothing happens."""
        if not self.settings.enabled:
            return False, "KI-Berater ist deaktiviert."
        return self.provider.is_configured()

    def reset_budget(self) -> None:
        self._calls_used = 0

    @property
    def calls_used(self) -> int:
        return self._calls_used

    @property
    def budget_left(self) -> int:
        return max(0, self.settings.max_calls_per_scan - self._calls_used)

    async def aclose(self) -> None:
        try:
            await self.provider.aclose()
        except Exception:  # pragma: no cover - best effort
            pass

    # -- decisions ---------------------------------------------------------- #

    def should_ask(self, confidence: float) -> bool:
        """Does this file warrant spending a call?"""
        if not self.available or self.budget_left <= 0:
            return False
        if self.settings.mode in ("all_candidates", "explain_only"):
            return True
        return confidence < self.settings.uncertain_below_confidence

    async def advise(self, context: dict[str, Any], filename: str | None = None) -> Advice:
        """Ask the configured backend about one file.  Never raises."""
        advice = Advice(
            provider=self.settings.provider,
            model=self.provider.describe_model(),
        )

        ready, reason = self.readiness()
        if not ready:
            advice.error = reason
            return advice

        async with self._lock:
            if self.budget_left <= 0:
                advice.error = "Anfrage-Budget fuer diesen Scan aufgebraucht"
                return advice
            self._calls_used += 1

        user = build_user_message(
            context, filename if self.settings.include_filename else None
        )

        try:
            raw = await self.provider.complete(
                SYSTEM_PROMPT, user, float(self.settings.timeout_seconds)
            )
        except AdvisorUnavailable as exc:
            advice.error = str(exc)
            self.last_error = advice.error
            return advice
        except asyncio.TimeoutError:
            advice.error = "Zeitueberschreitung bei der Anfrage."
            self.last_error = advice.error
            return advice
        except Exception as exc:
            advice.error = _describe(exc)
            self.last_error = advice.error
            log.warning("advisor call failed (%s): %s", self.settings.provider, exc)
            return advice

        if raw.model:
            advice.model = raw.model
        advice.input_tokens = raw.input_tokens
        advice.output_tokens = raw.output_tokens

        if raw.refused:
            advice.error = "Das Modell hat die Antwort abgelehnt - lokale Entscheidung gilt."
            self.last_error = advice.error
            return advice

        data = extract_json(raw.text)
        if data is None:
            advice.error = "Antwort enthielt kein verwertbares JSON."
            self.last_error = advice.error
            log.debug("unparseable advisor response: %s", raw.text[:400])
            return advice

        return sanitize(
            data,
            max_crf_delta=self.settings.max_crf_delta,
            allow_changes=(
                self.settings.allow_setting_changes and self.settings.mode != "explain_only"
            ),
            advice=advice,
        )

    async def test_connection(self) -> tuple[bool, str]:
        """Used by the 'Verbindung testen' button in the settings UI."""
        try:
            return await self.provider.check()
        except AdvisorUnavailable as exc:
            return False, str(exc)
        except Exception as exc:
            return False, _describe(exc)


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


# --------------------------------------------------------------------------- #
# Process-wide instance
# --------------------------------------------------------------------------- #

_advisor: Advisor | None = None


def _fingerprint(settings: AdvisorSettings) -> tuple:
    """Everything that, when changed, requires a fresh client."""
    return (
        settings.provider,
        settings.api_key,
        settings.model,
        settings.openai_base_url,
        settings.openai_api_key,
        settings.openai_model,
        settings.codex_model,
        settings.timeout_seconds,
    )


def get_advisor(settings: AdvisorSettings, force_new: bool = False) -> Advisor:
    """Shared advisor, rebuilt whenever the backend configuration changes."""
    global _advisor
    if _advisor is None or force_new or _fingerprint(_advisor.settings) != _fingerprint(settings):
        _advisor = Advisor(settings)
    else:
        # Behavioural knobs (budget, thresholds) can change without a new client.
        _advisor.settings = settings
        _advisor.provider.settings = settings  # type: ignore[attr-defined]
    return _advisor


def sdk_available() -> bool:
    """Kept for the system endpoint: is at least one backend usable?"""
    from .provider_anthropic import SDK_AVAILABLE as ANTHROPIC_SDK
    from .provider_openai import SDK_AVAILABLE as OPENAI_SDK

    return bool(ANTHROPIC_SDK or OPENAI_SDK)
