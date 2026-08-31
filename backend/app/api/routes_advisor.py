"""Advisor backends: which one is active, and signing in to the ones that need it."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete

from ..config import AdvisorSettings, load_settings
from ..core.advisor import get_advisor, provider_catalogue
from ..core.advisor import codex_oauth
from ..core.advisor.codex_oauth import OAuthError
from ..core.advisor.provider_codex import (
    KNOWN_MODELS,
    CodexProvider,
    clear_tokens,
    credential_status,
    store_tokens,
)
from ..core.advisor.provider_openai import normalise_base_url
from ..core.events import bus
from ..db import session_scope
from ..models import OAuthFlow, utcnow

log = logging.getLogger(__name__)
router = APIRouter()

#: A sign-in the user never finished is worthless after this long.
FLOW_TTL = dt.timedelta(minutes=30)


def _prune_flows() -> None:
    cutoff = utcnow() - FLOW_TTL
    try:
        with session_scope() as s:
            s.execute(delete(OAuthFlow).where(OAuthFlow.created_at < cutoff))
    except Exception:  # pragma: no cover - housekeeping must never fail a request
        log.debug("could not prune stale oauth flows", exc_info=True)


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

@router.get("/advisor/providers")
def list_providers() -> dict[str, Any]:
    """Everything the settings screen needs to render the provider section."""
    settings = load_settings()
    advisor = get_advisor(settings.advisor)
    ready, reason = advisor.readiness()
    return {
        "active": settings.advisor.provider,
        "enabled": settings.advisor.enabled,
        "ready": ready,
        "reason": reason,
        "providers": provider_catalogue(),
        "codex": {
            **credential_status(),
            "known_models": KNOWN_MODELS,
            "redirect_uri": codex_oauth.REDIRECT_URI,
        },
        "calls_used": advisor.calls_used,
        "budget_left": advisor.budget_left,
    }


class TestRequest(BaseModel):
    """Optional overrides so the user can test before saving."""

    provider: str | None = None
    api_key: str | None = None
    model: str | None = None
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None
    codex_model: str | None = None


@router.post("/advisor/test")
async def test_provider(payload: TestRequest | None = None) -> dict[str, Any]:
    """Run a real request against the configured (or supplied) backend."""
    settings = load_settings()
    cfg: AdvisorSettings = settings.advisor.model_copy(deep=True)

    if payload:
        for field in (
            "provider", "api_key", "model",
            "openai_base_url", "openai_api_key", "openai_model", "codex_model",
        ):
            value = getattr(payload, field, None)
            if value:
                setattr(cfg, field, value)
        if payload.openai_base_url:
            cfg.openai_base_url = normalise_base_url(payload.openai_base_url)

    # A throwaway advisor so the shared one keeps its budget and its clients.
    from ..core.advisor.service import Advisor

    probe = Advisor(cfg)
    try:
        ok, message = await probe.test_connection()
    finally:
        await probe.aclose()

    extra: dict[str, Any] = {}
    caps = getattr(probe.provider, "capabilities", None)
    if callable(caps):
        extra["capabilities"] = caps()
    return {"ok": ok, "message": message, "provider": cfg.provider, **extra}


@router.get("/advisor/openai/models")
async def list_openai_models(base_url: str = "", api_key: str = "") -> dict[str, Any]:
    """Ask an OpenAI-compatible endpoint what it can serve.

    Purely a convenience for the settings form - plenty of endpoints do not
    implement /models, and that is not an error.
    """
    settings = load_settings()
    url = normalise_base_url(base_url or settings.advisor.openai_base_url)
    key = api_key or settings.advisor.openai_api_key
    if not url:
        raise HTTPException(status_code=400, detail="Keine Endpunkt-URL angegeben.")

    try:
        import httpx
    except ImportError:
        raise HTTPException(status_code=500, detail="httpx fehlt im Container.")

    headers = {"Accept": "application/json"}
    if key.strip():
        headers["Authorization"] = f"Bearer {key.strip()}"

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(f"{url}/models", headers=headers)
    except Exception as exc:
        return {"ok": False, "models": [], "message": f"Nicht erreichbar: {type(exc).__name__}"}

    if response.status_code != 200:
        return {
            "ok": False,
            "models": [],
            "message": f"Endpunkt antwortet mit HTTP {response.status_code}"
                       + (" - API-Key pruefen." if response.status_code in (401, 403) else ""),
        }
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "models": [], "message": "Antwort war kein JSON."}

    models = sorted(
        {
            str(m.get("id"))
            for m in (data.get("data") or [])
            if isinstance(m, dict) and m.get("id")
        }
    )
    return {
        "ok": True,
        "models": models[:400],
        "message": f"{len(models)} Modelle gefunden." if models else "Keine Modelle gemeldet.",
    }


# --------------------------------------------------------------------------- #
# ChatGPT sign-in
# --------------------------------------------------------------------------- #

@router.post("/advisor/codex/start")
def codex_start() -> dict[str, Any]:
    """Begin the browser sign-in and hand back the link the user has to open."""
    _prune_flows()
    flow = codex_oauth.start_flow()
    with session_scope() as s:
        s.add(OAuthFlow(
            state=flow.state,
            provider="openai_codex",
            code_verifier=flow.code_verifier,
            redirect_uri=flow.redirect_uri,
        ))
    return {
        "authorize_url": flow.authorize_url,
        "state": flow.state,
        "redirect_uri": flow.redirect_uri,
        "instructions": (
            "Oeffne den Link, melde dich mit deinem ChatGPT-Konto an und bestaetige den "
            "Zugriff. Danach landet der Browser auf einer Seite, die nicht geladen werden "
            "kann - das ist richtig so. Kopiere die komplette Adresse aus der Adresszeile "
            "und fuege sie hier ein."
        ),
    }


class CodexCompleteRequest(BaseModel):
    pasted: str = Field(..., description="Die komplette Redirect-Adresse oder nur der Code")
    state: str | None = None


@router.post("/advisor/codex/complete")
async def codex_complete(payload: CodexCompleteRequest) -> dict[str, Any]:
    """Finish the sign-in with whatever the user pasted back."""
    try:
        code, state_from_url = codex_oauth.extract_code(payload.pasted)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    state = state_from_url or payload.state
    with session_scope() as s:
        flow = s.get(OAuthFlow, state) if state else None
        if flow is None:
            # Fall back to the most recent pending flow: a user who pasted only
            # the code has no state to offer, and refusing would be unhelpful.
            flow = (
                s.query(OAuthFlow)
                .filter(OAuthFlow.provider == "openai_codex")
                .order_by(OAuthFlow.created_at.desc())
                .first()
            )
        if flow is None:
            raise HTTPException(
                status_code=409,
                detail="Kein laufender Anmeldevorgang gefunden. Bitte die Anmeldung neu starten.",
            )
        verifier = flow.code_verifier
        redirect_uri = flow.redirect_uri or codex_oauth.REDIRECT_URI
        flow_state = flow.state

    try:
        tokens = await codex_oauth.exchange_code(code, verifier, redirect_uri)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await asyncio.to_thread(store_tokens, tokens)
    with session_scope() as s:
        s.execute(delete(OAuthFlow).where(OAuthFlow.state == flow_state))

    # Switch to this provider straight away - signing in means wanting to use it.
    from ..config import update_settings
    update_settings({"advisor": {"provider": "openai_codex", "enabled": True}})
    get_advisor(load_settings().advisor, force_new=True)

    status = credential_status()
    bus.publish("advisor.changed", {"provider": "openai_codex", "signed_in": True})
    return {
        "ok": True,
        "message": (
            f"Angemeldet als {status.get('account_label') or 'ChatGPT-Konto'}"
            + (f" ({status['plan_type']})" if status.get("plan_type") else "")
        ),
        "status": status,
    }


class CodexImportRequest(BaseModel):
    auth_json: str = Field(..., description="Inhalt von ~/.codex/auth.json")


@router.post("/advisor/codex/import")
async def codex_import(payload: CodexImportRequest) -> dict[str, Any]:
    """Take credentials straight from a local ``codex login``."""
    try:
        tokens = codex_oauth.tokens_from_auth_json(payload.auth_json)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # An access token that is already expired is fine as long as it can refresh.
    if tokens.is_expired and tokens.refresh_token:
        try:
            tokens = await codex_oauth.refresh_tokens(tokens)
        except OAuthError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Die Zugangsdaten liessen sich nicht erneuern: {exc}",
            ) from exc

    await asyncio.to_thread(store_tokens, tokens)
    from ..config import update_settings
    update_settings({"advisor": {"provider": "openai_codex", "enabled": True}})
    get_advisor(load_settings().advisor, force_new=True)

    status = credential_status()
    bus.publish("advisor.changed", {"provider": "openai_codex", "signed_in": True})
    warning = "" if tokens.account_id else (
        " Hinweis: In den Daten steckt keine Konto-Kennung - falls Anfragen abgelehnt "
        "werden, hilft die Anmeldung ueber den Browser."
    )
    return {
        "ok": True,
        "message": (
            f"Zugangsdaten uebernommen ({status.get('account_label') or 'ChatGPT-Konto'})."
            + warning
        ),
        "status": status,
    }


@router.post("/advisor/codex/logout")
def codex_logout() -> dict[str, Any]:
    """Forget the stored ChatGPT credentials."""
    clear_tokens()
    with session_scope() as s:
        s.execute(delete(OAuthFlow).where(OAuthFlow.provider == "openai_codex"))
    get_advisor(load_settings().advisor, force_new=True)
    bus.publish("advisor.changed", {"provider": "openai_codex", "signed_in": False})
    return {"ok": True, "message": "Abgemeldet."}


@router.get("/advisor/codex/status")
def codex_status() -> dict[str, Any]:
    return credential_status()


@router.get("/advisor/codex/models")
async def codex_models(refresh: bool = False) -> dict[str, Any]:
    """Model slugs this ChatGPT account can actually use.

    OpenAI rotates these and gates them per plan and per client, so the list is
    fetched rather than hard-coded; the built-in one is only the fallback.
    """
    settings = load_settings()
    advisor = get_advisor(settings.advisor)
    provider = advisor.provider
    if not isinstance(provider, CodexProvider):
        provider = CodexProvider(settings.advisor)
    ready, reason = provider.is_configured()
    if not ready:
        return {"ok": False, "models": KNOWN_MODELS, "message": reason}
    models, note = await provider.list_models(force=refresh)
    return {"ok": True, "models": models, "message": note}
