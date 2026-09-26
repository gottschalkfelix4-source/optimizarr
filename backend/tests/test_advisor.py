"""Advisor tests: response parsing, clamping, endpoint negotiation, OAuth helpers."""
import base64
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.gettempdir()) / "optimizarr-pytest"
os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(TMP / "config"))
os.environ.setdefault("OPTIMIZARR_TRANSCODE_DIR", str(TMP / "transcode"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import AdvisorSettings  # noqa: E402
from app.core.advisor import codex_oauth  # noqa: E402
from app.core.advisor.base import Advice, extract_json, sanitize  # noqa: E402
from app.core.advisor.provider_openai import (  # noqa: E402
    OpenAICompatibleProvider,
    normalise_base_url,
)
from app.core.advisor.service import build_provider, provider_catalogue  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    """Own in-memory database: stored credentials or settings left behind by
    another test (or a real install in the config dir) must not leak in."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app import config, db
    from app.models import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(config, "_cache", None)
    yield
    engine.dispose()


# --------------------------------------------------------------------------- #
# Response parsing - endpoints without schema enforcement return all sorts
# --------------------------------------------------------------------------- #

GOOD = {
    "content_type": "animation",
    "grain_assessment": "none",
    "crf_delta": 3,
    "film_grain_override": -1,
    "recommend_convert": True,
    "confidence": 0.8,
    "reasoning": "Flaechiger Anime.",
    "warnings": [],
}


def test_extract_json_plain():
    assert extract_json(json.dumps(GOOD)) == GOOD


def test_extract_json_from_markdown_fence():
    text = f"Hier ist das Ergebnis:\n```json\n{json.dumps(GOOD)}\n```\nViel Erfolg!"
    assert extract_json(text) == GOOD


def test_extract_json_after_preamble():
    text = f"Nach Analyse des Materials:\n{json.dumps(GOOD)}"
    assert extract_json(text) == GOOD


def test_extract_json_ignores_braces_inside_strings():
    payload = dict(GOOD, reasoning="Enthaelt { eine } Klammer im Text")
    assert extract_json(f"blah {json.dumps(payload)} blah") == payload


def test_extract_json_handles_nested_objects():
    nested = dict(GOOD, extra={"a": {"b": [1, 2]}})
    assert extract_json(json.dumps(nested))["extra"] == {"a": {"b": [1, 2]}}


def test_extract_json_returns_none_on_garbage():
    assert extract_json("Ich kann das leider nicht beantworten.") is None
    assert extract_json("") is None
    assert extract_json("{ kaputt") is None


# --------------------------------------------------------------------------- #
# Sanitising - the security boundary
# --------------------------------------------------------------------------- #

def test_sanitize_clamps_crf_delta():
    out = sanitize({**GOOD, "crf_delta": 99}, max_crf_delta=4, allow_changes=True)
    assert out.crf_delta == 4
    out = sanitize({**GOOD, "crf_delta": -99}, max_crf_delta=4, allow_changes=True)
    assert out.crf_delta == -4


def test_sanitize_blocks_changes_when_not_allowed():
    out = sanitize(
        {**GOOD, "crf_delta": 3, "film_grain_override": 20},
        max_crf_delta=4,
        allow_changes=False,
    )
    assert out.crf_delta == 0
    assert out.film_grain_override == -1
    # The explanation still comes through - explain_only mode relies on this.
    assert out.reasoning == "Flaechiger Anime."


def test_sanitize_survives_wrong_types():
    out = sanitize(
        {
            "content_type": 12345,
            "crf_delta": "not a number",
            "film_grain_override": None,
            "confidence": "hoch",
            "reasoning": None,
            "warnings": "einzelner String",
        },
        max_crf_delta=4,
        allow_changes=True,
    )
    assert out.crf_delta == 0
    assert out.film_grain_override == -1
    assert out.confidence == 0.0
    assert out.warnings == ["einzelner String"]
    assert out.ok


def test_sanitize_truncates_long_text():
    out = sanitize({**GOOD, "reasoning": "x" * 5000}, max_crf_delta=4, allow_changes=True)
    assert len(out.reasoning) <= 1200


def test_sanitize_accepts_float_crf_delta():
    # Endpoints without strict schemas happily return 3.0 instead of 3.
    out = sanitize({**GOOD, "crf_delta": 3.0}, max_crf_delta=4, allow_changes=True)
    assert out.crf_delta == 3


def test_sanitize_caps_grain_override():
    out = sanitize({**GOOD, "film_grain_override": 900}, max_crf_delta=4, allow_changes=True)
    assert out.film_grain_override == 50


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #

def test_all_three_providers_build():
    names = set()
    for pid in ("anthropic", "openai_compatible", "openai_codex"):
        provider = build_provider(AdvisorSettings(provider=pid))
        assert provider.name == pid
        names.add(provider.label)
    assert len(names) == 3


def test_catalogue_lists_every_provider():
    ids = {p["id"] for p in provider_catalogue()}
    assert ids == {"anthropic", "openai_compatible", "openai_codex"}


def test_unconfigured_providers_explain_themselves():
    for pid in ("anthropic", "openai_compatible", "openai_codex"):
        ready, reason = build_provider(AdvisorSettings(provider=pid)).is_configured()
        assert not ready
        assert reason  # never a silent failure


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://nas:11434", "http://nas:11434/v1"),
        ("http://nas:11434/", "http://nas:11434/v1"),
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        ("https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1"),
        ("nas:1234", "http://nas:1234/v1"),
        ("", ""),
    ],
)
def test_base_url_normalisation(raw, expected):
    assert normalise_base_url(raw) == expected


# --------------------------------------------------------------------------- #
# Endpoint negotiation
# --------------------------------------------------------------------------- #

def _provider_with(handler) -> OpenAICompatibleProvider:
    settings = AdvisorSettings(
        provider="openai_compatible",
        openai_base_url="http://endpoint.test/v1",
        openai_model="test-model",
        openai_api_key="key",
    )
    provider = OpenAICompatibleProvider(settings)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _ok_response(text: str = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "test-model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "choices": [{"message": {"content": text or json.dumps(GOOD)},
                         "finish_reason": "stop"}],
        },
    )


@pytest.mark.anyio
async def test_json_schema_is_tried_first_and_kept():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        fmt = body.get("response_format", {})
        seen.append((fmt.get("type"), (fmt.get("json_schema") or {}).get("strict")))
        return _ok_response()

    provider = _provider_with(handler)
    raw = await provider.complete("sys", "user", 30)
    assert seen == [("json_schema", True)]
    assert extract_json(raw.text) == GOOD
    assert provider.capabilities()["structured"] == "json_schema_strict"


@pytest.mark.anyio
async def test_falls_back_through_the_ladder_when_schema_rejected():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        fmt = body.get("response_format", {})
        seen.append((fmt.get("type"), (fmt.get("json_schema") or {}).get("strict")))
        if fmt.get("type") == "json_schema":
            return httpx.Response(
                400,
                json={"error": {"message": "response_format.json_schema is not supported"}},
            )
        return _ok_response()

    provider = _provider_with(handler)
    raw = await provider.complete("sys", "user", 30)
    # strict first, then the same field without the guarantee, then json_object
    assert seen == [("json_schema", True), ("json_schema", False), ("json_object", None)]
    assert extract_json(raw.text) == GOOD
    assert provider.capabilities()["structured"] == "json_object"


@pytest.mark.anyio
async def test_falls_back_all_the_way_to_prompt_only():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mode = body.get("response_format", {}).get("type")
        seen.append(mode)
        if mode is not None:
            return httpx.Response(
                400, json={"error": {"message": "response_format is not supported"}}
            )
        # Prompt-only servers wrap their JSON in prose and fences.
        return _ok_response(f"Sicher!\n```json\n{json.dumps(GOOD)}\n```")

    provider = _provider_with(handler)
    raw = await provider.complete("sys", "user", 30)
    assert seen == ["json_schema", "json_schema", "json_object", None]
    assert extract_json(raw.text) == GOOD
    assert provider.capabilities()["structured"] == "prompt"


@pytest.mark.anyio
async def test_learned_mode_is_reused_on_the_next_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mode = body.get("response_format", {}).get("type")
        calls.append(mode)
        if mode == "json_schema":
            return httpx.Response(400, json={"error": {"message": "json_schema unsupported"}})
        return _ok_response()

    provider = _provider_with(handler)
    await provider.complete("sys", "user", 30)
    calls.clear()
    await provider.complete("sys", "user", 30)
    # Second call must not re-probe the modes that already failed.
    assert calls == ["json_object"]


@pytest.mark.anyio
async def test_switches_to_max_completion_tokens_when_told_to():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(sorted(k for k in body if "token" in k))
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {
                "message": "Unsupported parameter: 'max_tokens' is not supported with this "
                           "model. Use 'max_completion_tokens' instead.",
            }})
        return _ok_response()

    provider = _provider_with(handler)
    raw = await provider.complete("sys", "user", 30)
    assert seen == [["max_tokens"], ["max_completion_tokens"]]
    assert extract_json(raw.text) == GOOD
    assert provider.capabilities()["token_field"] == "max_completion_tokens"


@pytest.mark.anyio
async def test_drops_temperature_when_the_model_refuses_it():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {
                "message": "Unsupported value: 'temperature' does not support 0.2",
            }})
        return _ok_response()

    provider = _provider_with(handler)
    await provider.complete("sys", "user", 30)
    assert provider.capabilities()["send_temperature"] is False


@pytest.mark.anyio
async def test_auth_failure_is_reported_not_retried():
    from app.core.advisor.base import AdvisorUnavailable

    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    provider = _provider_with(handler)
    with pytest.raises(AdvisorUnavailable, match="API-Key"):
        await provider.complete("sys", "user", 30)
    assert len(attempts) == 1  # no pointless retry ladder on a bad key


@pytest.mark.anyio
async def test_unreachable_endpoint_gives_an_actionable_message():
    from app.core.advisor.base import AdvisorUnavailable

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _provider_with(handler)
    with pytest.raises(AdvisorUnavailable, match="nicht erreichbar"):
        await provider.complete("sys", "user", 30)


@pytest.mark.anyio
async def test_reads_content_returned_as_parts():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"message": {"content": [
                {"type": "text", "text": json.dumps(GOOD)},
            ]}}],
        })

    provider = _provider_with(handler)
    raw = await provider.complete("sys", "user", 30)
    assert extract_json(raw.text) == GOOD


@pytest.mark.anyio
async def test_reads_answer_from_a_tool_call():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"message": {
                "content": None,
                "tool_calls": [{"function": {"name": "advice",
                                             "arguments": json.dumps(GOOD)}}],
            }}],
        })

    provider = _provider_with(handler)
    raw = await provider.complete("sys", "user", 30)
    assert extract_json(raw.text) == GOOD


@pytest.mark.anyio
async def test_system_role_is_folded_into_user_when_rejected():
    roles_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        roles = [m["role"] for m in body["messages"]]
        roles_seen.append(roles)
        if "system" in roles:
            return httpx.Response(400, json={"error": {
                "message": "The system role is not supported by this model",
            }})
        return _ok_response()

    provider = _provider_with(handler)
    await provider.complete("SYSTEMTEXT", "USERTEXT", 30)
    assert roles_seen[0] == ["system", "user"]
    assert roles_seen[-1] == ["user"]


# --------------------------------------------------------------------------- #
# End-to-end through the Advisor, including clamping
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_advisor_clamps_what_a_rogue_endpoint_returns():
    from app.core.advisor.service import Advisor

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(json.dumps({
            **GOOD,
            "crf_delta": 40,               # way past the limit
            "film_grain_override": 999,
            "reasoning": "y" * 4000,
        }))

    settings = AdvisorSettings(
        enabled=True,
        provider="openai_compatible",
        openai_base_url="http://endpoint.test/v1",
        openai_model="m",
        max_crf_delta=4,
    )
    advisor = Advisor(settings)
    advisor.provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    advice = await advisor.advise({"source": {"codec": "h264"}}, filename="Film.mkv")
    assert advice.ok
    assert advice.crf_delta == 4
    assert advice.film_grain_override == 50
    assert len(advice.reasoning) <= 1200
    assert advice.provider == "openai_compatible"


@pytest.mark.anyio
async def test_advisor_never_raises_when_the_endpoint_dies():
    from app.core.advisor.service import Advisor

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="kaputt")

    settings = AdvisorSettings(
        enabled=True, provider="openai_compatible",
        openai_base_url="http://endpoint.test/v1", openai_model="m",
    )
    advisor = Advisor(settings)
    advisor.provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    advice = await advisor.advise({}, filename="x.mkv")
    assert not advice.ok
    assert advice.error  # explained, not raised
    assert advice.crf_delta == 0


@pytest.mark.anyio
async def test_budget_is_enforced():
    from app.core.advisor.service import Advisor

    settings = AdvisorSettings(
        enabled=True, provider="openai_compatible",
        openai_base_url="http://endpoint.test/v1", openai_model="m",
        max_calls_per_scan=2,
    )
    advisor = Advisor(settings)
    advisor.provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: _ok_response())
    )
    for _ in range(2):
        assert (await advisor.advise({})).ok
    assert advisor.budget_left == 0
    assert not advisor.should_ask(0.1)
    spent = await advisor.advise({})
    assert not spent.ok and "Budget" in spent.error


def test_should_ask_respects_the_mode():
    from app.core.advisor.service import Advisor

    base = dict(
        enabled=True, provider="openai_compatible",
        openai_base_url="http://x/v1", openai_model="m",
        uncertain_below_confidence=0.6,
    )
    uncertain = Advisor(AdvisorSettings(**base, mode="uncertain_only"))
    assert uncertain.should_ask(0.4)
    assert not uncertain.should_ask(0.9)

    always = Advisor(AdvisorSettings(**base, mode="all_candidates"))
    assert always.should_ask(0.99)

    disabled = Advisor(AdvisorSettings(**{**base, "enabled": False}, mode="all_candidates"))
    assert not disabled.should_ask(0.1)


# --------------------------------------------------------------------------- #
# OAuth helpers
# --------------------------------------------------------------------------- #

def test_pkce_challenge_matches_the_verifier():
    import hashlib

    flow = codex_oauth.start_flow()
    assert 43 <= len(flow.code_verifier) <= 128
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(flow.code_verifier.encode()).digest()
    ).decode().rstrip("=")
    assert f"code_challenge={expected}" in flow.authorize_url
    assert "code_challenge_method=S256" in flow.authorize_url
    assert f"state={flow.state}" in flow.authorize_url


def test_two_flows_never_share_a_verifier():
    a, b = codex_oauth.start_flow(), codex_oauth.start_flow()
    assert a.code_verifier != b.code_verifier
    assert a.state != b.state


@pytest.mark.parametrize(
    "pasted",
    [
        "http://localhost:1455/auth/callback?code=abc123def456&state=xyz",
        "https://localhost:1455/auth/callback?state=xyz&code=abc123def456",
        "?code=abc123def456&state=xyz",
        "code=abc123def456&state=xyz",
    ],
)
def test_extract_code_accepts_every_paste_shape(pasted):
    code, state = codex_oauth.extract_code(pasted)
    assert code == "abc123def456"
    assert state == "xyz"


def test_extract_code_accepts_a_bare_code():
    code, state = codex_oauth.extract_code("  abc123def456ghi  ")
    assert code == "abc123def456ghi"
    assert state is None


def test_extract_code_reports_a_denied_login():
    with pytest.raises(codex_oauth.OAuthError, match="abgelehnt"):
        codex_oauth.extract_code(
            "http://localhost:1455/auth/callback?error=access_denied"
            "&error_description=User+declined"
        )


def test_extract_code_rejects_obvious_mistakes():
    with pytest.raises(codex_oauth.OAuthError):
        codex_oauth.extract_code("")
    with pytest.raises(codex_oauth.OAuthError):
        codex_oauth.extract_code("nope")
    with pytest.raises(codex_oauth.OAuthError, match="code"):
        codex_oauth.extract_code("http://localhost:1455/auth/callback?state=xyz")


def _fake_jwt(claims: dict) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


def test_decode_jwt_claims_reads_the_payload():
    token = _fake_jwt({"email": "felix@example.com", "exp": 1234567890})
    claims = codex_oauth.decode_jwt_claims(token)
    assert claims["email"] == "felix@example.com"


def test_decode_jwt_claims_survives_garbage():
    assert codex_oauth.decode_jwt_claims("") == {}
    assert codex_oauth.decode_jwt_claims("not.a.jwt") == {}
    assert codex_oauth.decode_jwt_claims("onlyonepart") == {}


def test_auth_json_import_nested_layout():
    token = _fake_jwt({
        "email": "felix@example.com",
        codex_oauth.AUTH_CLAIM: {
            "chatgpt_account_id": "acct-123",
            "chatgpt_plan_type": "plus",
        },
    })
    raw = json.dumps({"tokens": {
        "access_token": "at", "refresh_token": "rt", "id_token": token,
    }})
    tokens = codex_oauth.tokens_from_auth_json(raw)
    assert tokens.access_token == "at"
    assert tokens.refresh_token == "rt"
    assert tokens.account_id == "acct-123"
    assert tokens.plan_type == "plus"
    assert tokens.account_label == "felix@example.com"


def test_auth_json_import_flat_layout():
    tokens = codex_oauth.tokens_from_auth_json(
        json.dumps({"access_token": "at", "refresh_token": "rt"})
    )
    assert tokens.access_token == "at"


def test_auth_json_import_rejects_unrelated_files():
    with pytest.raises(codex_oauth.OAuthError):
        codex_oauth.tokens_from_auth_json("{}")
    with pytest.raises(codex_oauth.OAuthError):
        codex_oauth.tokens_from_auth_json("kein json")
    with pytest.raises(codex_oauth.OAuthError):
        codex_oauth.tokens_from_auth_json(json.dumps({"unrelated": "value"}))


def test_token_expiry_uses_a_safety_margin():
    from app.core.advisor.codex_oauth import TokenSet

    now = dt.datetime.now(dt.timezone.utc)
    assert TokenSet(access_token="a", expires_at=now + dt.timedelta(hours=2)).is_expired is False
    # Inside the refresh margin counts as expired, so renewal happens early.
    assert TokenSet(access_token="a", expires_at=now + dt.timedelta(minutes=2)).is_expired is True
    assert TokenSet(access_token="a", expires_at=now - dt.timedelta(minutes=1)).is_expired is True
    assert TokenSet(access_token="a", expires_at=None).is_expired is False


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------- #
# Stored secrets only go where they were configured for
# --------------------------------------------------------------------------- #

STORED_URL = "https://api.openai.com/v1"
HEADERS = {"X-Optimizarr": "1"}


def _store_advisor(**fields):
    from app.config import AppSettings, save_settings

    settings = AppSettings()
    for key, value in fields.items():
        setattr(settings.advisor, key, value)
    save_settings(settings)


def _capture_http(monkeypatch):
    """Route every httpx.AsyncClient through a recorder."""
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


@pytest.fixture()
def api():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)   # no lifespan: no worker, no scan


def test_openai_key_rules():
    from app.api.routes_advisor import openai_key_for

    stored = AdvisorSettings(openai_base_url=STORED_URL, openai_api_key="sk-stored")
    assert openai_key_for(STORED_URL + "/", None, stored) == "sk-stored"
    assert openai_key_for(STORED_URL, "********", stored) == "sk-stored"
    assert openai_key_for("http://evil.example/v1", "", stored) == ""
    assert openai_key_for("http://evil.example/v1", "********", stored) == ""
    assert openai_key_for("http://evil.example/v1", "sk-own", stored) == "sk-own"
    assert openai_key_for(STORED_URL, None, AdvisorSettings(openai_api_key="sk")) == ""


def test_model_list_is_a_post_and_keeps_the_stored_key_home(api, monkeypatch):
    _store_advisor(openai_base_url=STORED_URL, openai_api_key="sk-stored")
    seen = _capture_http(monkeypatch)

    assert api.get("/api/advisor/openai/models", params={"base_url": "http://evil.example"}).status_code == 405

    r = api.post("/api/advisor/openai/models", headers=HEADERS,
                 json={"base_url": "http://evil.example/v1", "api_key": "********"})
    assert r.status_code == 200 and r.json()["models"] == ["m1"]
    assert str(seen[-1].url) == "http://evil.example/v1/models"
    assert "authorization" not in seen[-1].headers

    api.post("/api/advisor/openai/models", headers=HEADERS,
             json={"base_url": STORED_URL + "/", "api_key": None})
    assert seen[-1].headers["authorization"] == "Bearer sk-stored"


def test_connection_test_does_not_ship_the_stored_key_elsewhere(api, monkeypatch):
    from app.core.advisor import service

    _store_advisor(openai_base_url=STORED_URL, openai_api_key="sk-stored", api_key="sk-ant")
    used: list[AdvisorSettings] = []

    async def fake_test(self):
        used.append(self.settings)
        return True, "ok"

    monkeypatch.setattr(service.Advisor, "test_connection", fake_test)
    api.post("/api/advisor/test", headers=HEADERS, json={
        "provider": "openai_compatible", "openai_base_url": "http://evil.example/v1",
        "openai_model": "m", "openai_api_key": "********",
    })
    api.post("/api/advisor/test", headers=HEADERS, json={
        "provider": "openai_compatible", "openai_model": "m", "openai_api_key": "********",
    })
    api.post("/api/advisor/test", headers=HEADERS, json={"provider": "anthropic", "api_key": "********"})
    evil, home, anthropic = used
    assert evil.openai_base_url == "http://evil.example/v1" and evil.openai_api_key == ""
    assert home.openai_api_key == "sk-stored"
    # The mask means "not given", never the key itself.
    assert anthropic.api_key == "sk-ant"


# --------------------------------------------------------------------------- #
# Claude: per-model request shape
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model,effort,adaptive", [
    ("claude-opus-5-5", True, True),
    ("claude-opus-5", True, True),
    ("claude-sonnet-5", True, True),
    ("claude-fable-5-1", True, True),
    ("claude-haiku-4-5", False, False),
    ("claude-opus-4-5", True, False),
    ("claude-sonnet-4-5", False, False),
])
def test_model_features(model, effort, adaptive):
    from app.core.advisor.provider_anthropic import model_features

    assert model_features(model) == (effort, adaptive)


class _FakeMessages:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        from types import SimpleNamespace

        self.calls.append(kwargs)
        return SimpleNamespace(
            model=kwargs["model"], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            content=[SimpleNamespace(type="text", text=json.dumps(GOOD))],
        )


def _anthropic(model):
    from types import SimpleNamespace

    from app.core.advisor.provider_anthropic import SDK_AVAILABLE, AnthropicProvider

    if not SDK_AVAILABLE:
        pytest.skip("anthropic SDK not installed")
    provider = AnthropicProvider(AdvisorSettings(api_key="sk", model=model))
    messages = _FakeMessages()
    provider._client = SimpleNamespace(messages=messages)
    return provider, messages


@pytest.mark.anyio
async def test_haiku_gets_neither_effort_nor_adaptive_thinking():
    provider, messages = _anthropic("claude-haiku-4-5")
    await provider.complete("sys", "user", 30)
    ok, _ = await provider.check()
    request, check = messages.calls
    assert "thinking" not in request
    assert "effort" not in request["output_config"]
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "output_config" not in check
    assert ok


@pytest.mark.anyio
async def test_current_models_keep_low_effort_and_adaptive_thinking():
    provider, messages = _anthropic("claude-opus-5-5")
    await provider.complete("sys", "user", 30)
    await provider.check()
    request, check = messages.calls
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"]["effort"] == "low"
    assert check["output_config"] == {"effort": "low"}
    # The prompt is below every model's cache minimum - no marker pretending otherwise.
    assert "cache_control" not in json.dumps(request["system"])


# --------------------------------------------------------------------------- #
# OpenAI-compatible connection test wording
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_check_names_the_strict_schema_mode():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        return _ok_response('{"ok": true}')

    ok, message = await _provider_with(handler).check()
    assert ok and "mit erzwungenem JSON-Schema" in message
    assert "json_schema_strict" not in message


@pytest.mark.anyio
async def test_check_reports_a_dropped_system_role_on_its_own():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(404)
        body = json.loads(request.content)
        if any(m["role"] == "system" for m in body["messages"]):
            return httpx.Response(400, json={"error": {"message": "system role not supported"}})
        return _ok_response('{"ok": true}')

    ok, message = await _provider_with(handler).check()
    assert ok and "[angepasst: ohne system-Rolle]" in message
