"""Values verified against the public Codex client, plus the traps around them.

A wrong constant here does not fail loudly - it fails as "sign-in rejected" or
"403 on every request", which is expensive to debug. So they are pinned.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

TMP = Path(tempfile.gettempdir()) / "optimizarr-pytest"
os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(TMP / "config"))
os.environ.setdefault("OPTIMIZARR_TRANSCODE_DIR", str(TMP / "transcode"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import AdvisorSettings  # noqa: E402
from app.core.advisor import codex_oauth  # noqa: E402
from app.core.advisor.base import AdvisorUnavailable, extract_json  # noqa: E402
from app.core.advisor.codex_oauth import TokenSet  # noqa: E402
from app.core.advisor.provider_codex import (  # noqa: E402
    FALLBACK_MODELS,
    CodexProvider,
)
from app.core.advisor.provider_openai import OpenAICompatibleProvider  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


# --------------------------------------------------------------------------- #
# OAuth constants
# --------------------------------------------------------------------------- #

def test_oauth_constants_match_the_codex_client():
    assert codex_oauth.CLIENT_ID == "app_EMoamEEZ73f0CkXaXp7hrann"
    assert codex_oauth.ISSUER == "https://auth.openai.com"
    assert codex_oauth.AUTHORIZE_URL == "https://auth.openai.com/oauth/authorize"
    assert codex_oauth.TOKEN_URL == "https://auth.openai.com/oauth/token"
    assert codex_oauth.REDIRECT_URI == "http://localhost:1455/auth/callback"
    assert codex_oauth.ORIGINATOR == "codex_cli_rs"
    # The two connector scopes are part of the set the client asks for; a
    # different scope set risks a rejected authorization.
    assert codex_oauth.SCOPES.split() == [
        "openid", "profile", "email", "offline_access",
        "api.connectors.read", "api.connectors.invoke",
    ]


def test_authorize_url_carries_every_required_parameter():
    flow = codex_oauth.start_flow()
    query = parse_qs(urlparse(flow.authorize_url).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [codex_oauth.CLIENT_ID]
    assert query["redirect_uri"] == [codex_oauth.REDIRECT_URI]
    assert query["scope"] == [codex_oauth.SCOPES]
    assert query["code_challenge_method"] == ["S256"]
    # Without these the id_token comes back without the account id.
    assert query["id_token_add_organizations"] == ["true"]
    assert query["codex_cli_simplified_flow"] == ["true"]
    assert query["originator"] == ["codex_cli_rs"]
    # Parameters the real client does not send must not appear.
    for absent in ("prompt", "nonce", "audience", "response_mode"):
        assert absent not in query


def test_scope_separator_is_percent_encoded():
    # The client percent-encodes values; urlencode's default would emit '+'.
    flow = codex_oauth.start_flow()
    scope_part = flow.authorize_url.split("scope=")[1].split("&")[0]
    assert scope_part.startswith("openid%20profile%20email")
    assert "+" not in scope_part


def test_state_check_tolerates_the_server_suffix():
    # The authorization server appends this for some accounts; a plain equality
    # check would reject a perfectly good sign-in.
    expected = "abc123"
    suffix = ".onboarding_entrypoint=life_sciences"
    assert codex_oauth.state_matches(expected, expected)
    assert codex_oauth.state_matches(expected + suffix, expected)
    assert codex_oauth.strip_state_suffix(expected + suffix) == expected

    # Anything else must still be rejected.
    assert not codex_oauth.state_matches("voellig anders", expected)
    assert not codex_oauth.state_matches(expected + ".onboarding_entrypoint=unknown", expected)
    assert not codex_oauth.state_matches(expected + suffix + suffix, expected)
    assert not codex_oauth.state_matches(None, expected)


def test_secrets_are_redacted_from_text():
    line = (
        "GET /auth/callback?code=SECRET123&state=xyz "
        "access_token=eyJ0eXA refresh_token=rt_abc harmless=yes"
    )
    out = codex_oauth.redact(line)
    for secret in ("SECRET123", "eyJ0eXA", "rt_abc"):
        assert secret not in out
    assert "harmless=yes" in out


# --------------------------------------------------------------------------- #
# Request shape for the ChatGPT backend
# --------------------------------------------------------------------------- #

def test_request_body_obeys_the_backend_constraints():
    provider = CodexProvider(
        AdvisorSettings(provider="openai_codex", codex_model="gpt-5.6-sol")
    )
    body = provider._build_body("SYS", "USER")

    assert body["store"] is False           # the endpoint requires this
    assert body["stream"] is True           # there is no non-streaming mode
    assert "reasoning.encrypted_content" in body["include"]
    assert "max_output_tokens" not in body  # rejected outright
    assert body["model"] == "gpt-5.6-sol"
    # Responses API shape, not chat completions.
    assert body["input"][0]["content"][0]["type"] == "input_text"


def test_schema_is_flat_not_nested():
    provider = CodexProvider(AdvisorSettings(provider="openai_codex"))
    fmt = provider._build_body("SYS", "USER", with_schema=True)["text"]["format"]
    # name/strict/schema sit directly in `format` here. Copying the nested
    # chat-completions shape gets it rejected or silently ignored.
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert fmt["name"]
    assert fmt["schema"]["type"] == "object"
    assert "json_schema" not in fmt

    without = provider._build_body("SYS", "USER", with_schema=False)
    assert "text" not in without
    # The schema still reaches the model through the instructions.
    assert "content_type" in without["instructions"]


def test_headers_avoid_the_known_traps():
    provider = CodexProvider(AdvisorSettings(provider="openai_codex"))
    headers = provider._headers(TokenSet(access_token="at", account_id="acct-1"))

    assert headers["Authorization"] == "Bearer at"   # access token, not id token
    assert headers["originator"] == "codex_cli_rs"   # outside the allow-list means 403
    assert headers["ChatGPT-Account-ID"] == "acct-1"
    assert "session-id" in headers                   # hyphen; underscore form is dead
    assert "session_id" not in headers
    assert "OpenAI-Beta" not in headers              # no longer part of this path
    assert headers["User-Agent"].startswith("codex_cli_rs/")


def test_headers_survive_a_missing_account_id():
    provider = CodexProvider(AdvisorSettings(provider="openai_codex"))
    headers = provider._headers(TokenSet(access_token="at"))
    assert "ChatGPT-Account-ID" not in headers
    assert headers["Authorization"] == "Bearer at"


def test_fallback_model_list_has_no_retired_slugs():
    assert "gpt-5-codex" not in FALLBACK_MODELS      # historical slug
    assert "codex-mini-latest" not in FALLBACK_MODELS
    assert all(m.startswith("gpt-5") for m in FALLBACK_MODELS)


def test_model_not_found_is_explained_as_gating():
    from app.core.advisor.provider_codex import _explain_status

    message = _explain_status(404, "Model not found: gpt-5.6-sol")
    # The unhelpful reading is "that model does not exist"; the useful one is
    # that OpenAI gates models per client.
    assert "Liste abrufen" in message or "gemeldete Liste" in message


def test_schema_rejection_is_recognised():
    from app.core.advisor.provider_codex import _looks_like_schema_rejection

    assert _looks_like_schema_rejection(400, "unknown field: text.format")
    assert _looks_like_schema_rejection(422, "json_schema is not supported")
    # A rate limit is not a schema problem.
    assert not _looks_like_schema_rejection(429, "too many requests")
    assert not _looks_like_schema_rejection(500, "internal error")


# --------------------------------------------------------------------------- #
# The trap HTTP 200 hides
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


def _ok_response(text=None) -> httpx.Response:
    return httpx.Response(200, json={
        "model": "test-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        "choices": [{"message": {"content": text or json.dumps(GOOD)},
                     "finish_reason": "stop"}],
    })


@pytest.mark.anyio
async def test_silently_ignored_schema_still_steps_down():
    """An endpoint that accepts response_format and then ignores it.

    This is the failure a status check cannot see: HTTP 200 with prose where
    JSON was promised. The ladder has to keep descending until something parses.
    """
    modes = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        modes.append(body.get("response_format", {}).get("type"))
        if body.get("response_format", {}).get("type") == "json_schema":
            return _ok_response("Klar! Das ist ein Zeichentrickfilm, CRF etwas hoeher.")
        return _ok_response()

    provider = _provider_with(handler)
    raw = await provider.complete("sys", "user", 30)
    assert modes == ["json_schema", "json_schema", "json_object"]
    assert extract_json(raw.text) == GOOD


@pytest.mark.anyio
async def test_truncated_answer_is_reported_as_a_length_problem():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"message": {"content": '{"content_type": "anim'},
                         "finish_reason": "length"}],
        })

    provider = _provider_with(handler)
    with pytest.raises(AdvisorUnavailable, match="abgeschnitten"):
        await provider.complete("sys", "user", 30)


@pytest.mark.anyio
async def test_refusal_is_read_before_the_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": "m",
            "choices": [{"message": {"content": None, "refusal": "Kann ich nicht."},
                         "finish_reason": "stop"}],
        })

    provider = _provider_with(handler)
    raw = await provider.complete("sys", "user", 30)
    assert raw.refused


@pytest.mark.anyio
async def test_a_broken_schema_of_ours_is_raised_not_downgraded():
    """If our own schema is malformed, stepping down would hide the bug."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, json={"error": {
            "message": "Invalid schema for response_format: required fields are missing",
        }})

    provider = _provider_with(handler)
    with pytest.raises(AdvisorUnavailable, match="Anfrage-Schema"):
        await provider.complete("sys", "user", 30)
    assert len(attempts) == 1


# --------------------------------------------------------------------------- #
# SSE reassembly
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_sse_stream_is_reassembled_from_deltas():
    from app.core.advisor.provider_codex import _read_sse

    payload = json.dumps(GOOD)
    events = [
        'data: {"type":"response.created","response":{}}',
        f'data: {{"type":"response.output_text.delta","delta":{json.dumps(payload[:20])}}}',
        f'data: {{"type":"response.output_text.delta","delta":{json.dumps(payload[20:])}}}',
        'data: {"type":"response.completed","response":{"usage":'
        '{"input_tokens":100,"output_tokens":50}}}',
        "data: [DONE]",
    ]

    class FakeStream:
        async def aiter_lines(self):
            for line in events:
                yield line

    text, inp, out = await _read_sse(FakeStream())
    assert extract_json(text) == GOOD
    assert (inp, out) == (100, 50)


@pytest.mark.anyio
async def test_sse_falls_back_to_the_completed_object():
    """Some responses arrive whole rather than as deltas."""
    from app.core.advisor.provider_codex import _read_sse

    completed = {
        "type": "response.completed",
        "response": {
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(GOOD)},
            ]}],
            "usage": {"input_tokens": 5, "output_tokens": 7},
        },
    }

    class FakeStream:
        async def aiter_lines(self):
            yield f"data: {json.dumps(completed)}"
            yield "data: [DONE]"

    text, inp, out = await _read_sse(FakeStream())
    assert extract_json(text) == GOOD
    assert (inp, out) == (5, 7)


@pytest.mark.anyio
async def test_sse_reports_a_failed_response():
    from app.core.advisor.provider_codex import _read_sse

    class FakeStream:
        async def aiter_lines(self):
            yield ('data: {"type":"response.failed","response":{"error":'
                   '{"message":"model overloaded"}}}')

    with pytest.raises(AdvisorUnavailable, match="model overloaded"):
        await _read_sse(FakeStream())


@pytest.mark.anyio
async def test_sse_ignores_comments_and_unknown_events():
    from app.core.advisor.provider_codex import _read_sse

    class FakeStream:
        async def aiter_lines(self):
            yield ": heartbeat"
            yield ""
            yield 'data: {"type":"response.some_future_event","payload":{}}'
            yield f'data: {{"type":"response.output_text.delta","delta":{json.dumps(json.dumps(GOOD))}}}'
            yield "data: [DONE]"

    text, _, _ = await _read_sse(FakeStream())
    assert extract_json(text) == GOOD
