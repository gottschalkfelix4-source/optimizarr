"""Browser sign-in with a ChatGPT account, the way the Codex CLI does it.

**The problem this has to solve.**  The Codex CLI runs on the same machine as
the browser, so it can listen on ``localhost:1455`` and catch the OAuth redirect
itself.  Optimizarr runs in a container on a NAS while the browser is on a
laptop somewhere else, so nothing in the container will ever see that redirect.

The way out is the standard headless-OAuth pattern: Optimizarr builds the
authorize URL and shows it, the user signs in, the browser lands on a page that
cannot load (nothing is listening on their machine either), and the user copies
that failed URL back - it carries the ``code`` in its query string.  Optimizarr
then does the token exchange itself.  The redirect never has to succeed; only
its URL matters.

For anyone who does have the CLI installed there is a second path: paste the
contents of ``~/.codex/auth.json`` and skip the dance entirely.

Everything security-relevant is standard PKCE (RFC 7636): a random verifier
stays in the database, only its SHA-256 hash goes to the authorization server,
and the ``state`` parameter ties the callback back to the flow that started it.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

log = logging.getLogger(__name__)

try:
    import httpx
    HTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    HTTP_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Endpoint constants
#
# These mirror the public Codex CLI client.  They are the one part of this file
# that can rot: if OpenAI changes the flow, sign-in starts failing with a clear
# error from the authorization server rather than silently misbehaving.  The
# environment overrides match the ones the CLI honours, so enterprise or
# self-hosted issuers keep working.
# --------------------------------------------------------------------------- #
CLIENT_ID = os.environ.get(
    "CODEX_APP_SERVER_LOGIN_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann"
)
ISSUER = os.environ.get("CODEX_ISSUER_OVERRIDE", "https://auth.openai.com").rstrip("/")
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
TOKEN_URL = os.environ.get("CODEX_REFRESH_TOKEN_URL_OVERRIDE", f"{ISSUER}/oauth/token")

#: The authorization server only accepts redirect URIs from its own allow-list,
#: and localhost:1455 is the one the Codex client registered.  We never receive
#: this callback ourselves (see the module docstring) - it only has to match
#: byte for byte between the authorize request and the token exchange.
REDIRECT_URI = "http://localhost:1455/auth/callback"

#: Exactly the scope string the CLI requests.  The two api.connectors scopes are
#: part of it; asking for a different set risks a rejected authorization.
SCOPES = (
    "openid profile email offline_access "
    "api.connectors.read api.connectors.invoke"
)

#: Sent so the id_token carries the organisation claims - without them there is
#: no chatgpt_account_id, and the API rejects the request.
ORIGINATOR = os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "codex_cli_rs")

#: The authorization server may append this to the state it echoes back.  A
#: plain equality check would then fail to recognise our own flow.
STATE_SUFFIXES = (".onboarding_entrypoint=life_sciences",)

#: Claim namespace that carries the ChatGPT account id inside the id_token.
AUTH_CLAIM = "https://api.openai.com/auth"

#: Refresh this long before the access token actually expires.
REFRESH_MARGIN = dt.timedelta(minutes=10)

#: Never let these reach a log line or an API response.
SECRET_PARAMS = frozenset({
    "code", "code_verifier", "access_token", "id_token", "refresh_token",
    "state", "client_secret", "api_key", "subject_token", "requested_token",
})


class OAuthError(RuntimeError):
    """Anything that goes wrong during sign-in, with a message fit for the UI."""


# --------------------------------------------------------------------------- #
# PKCE
# --------------------------------------------------------------------------- #

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_verifier() -> str:
    """A high-entropy code verifier (RFC 7636 wants 43-128 characters)."""
    return _b64url(secrets.token_bytes(64))


def challenge_for(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass
class PendingFlow:
    state: str
    code_verifier: str
    authorize_url: str
    redirect_uri: str = REDIRECT_URI


def start_flow() -> PendingFlow:
    """Build the authorize URL the user has to open.

    Parameter order and encoding follow the CLI exactly: values are
    percent-encoded (so the scope separator is ``%20``, not ``+``), which is
    what ``quote`` gives us and ``urlencode``'s default ``quote_plus`` does not.
    """
    verifier = generate_verifier()
    state = _b64url(secrets.token_bytes(32))
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
        # Without these two the id_token comes back missing the organisation
        # claims, and the account id they carry is required for API calls.
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": ORIGINATOR,
    }
    query = urlencode(params, quote_via=quote, safe="")
    return PendingFlow(
        state=state,
        code_verifier=verifier,
        authorize_url=f"{AUTHORIZE_URL}?{query}",
    )


def state_matches(returned: str | None, expected: str) -> bool:
    """Compare the echoed state, tolerating the suffix the server may append.

    A plain equality check looks right and fails in production: the
    authorization server appends an onboarding marker to the state for some
    accounts, which would make a correct sign-in look like a forgery.
    """
    if not returned:
        return False
    if returned == expected:
        return True
    for suffix in STATE_SUFFIXES:
        if returned.endswith(suffix) and returned[: -len(suffix)] == expected:
            return True
    return False


def strip_state_suffix(returned: str) -> str:
    """The bare state, with any server-added suffix removed."""
    for suffix in STATE_SUFFIXES:
        if returned.endswith(suffix):
            return returned[: -len(suffix)]
    return returned


_REDACT_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in sorted(SECRET_PARAMS)) + r")=[^&\s\"']+"
)


def redact(text: str) -> str:
    """Blank out secrets before anything reaches a log or an error message.

    These tokens are password-equivalent - OpenAI's own guidance is to treat
    ``auth.json`` like a password - so nothing carrying them may be echoed.
    """
    return _REDACT_RE.sub(r"\1=<entfernt>", text or "")


def extract_code(pasted: str) -> tuple[str, str | None]:
    """Pull ``code`` (and ``state``) out of whatever the user pasted.

    Accepts the whole redirect URL, a bare query string, or just the code -
    people paste all three, and the difference is not worth an error message.
    """
    value = (pasted or "").strip()
    if not value:
        raise OAuthError("Es wurde nichts eingefuegt.")

    # Anything that carries parameters is treated as a redirect - which matters,
    # because a *failed* sign-in also comes back as a URL, just with `error=`
    # instead of `code=`.  Deciding on the presence of "code=" alone would wave
    # those through as if they were the code itself.
    looks_like_redirect = "://" in value or value.startswith(("/", "?")) or "=" in value
    if looks_like_redirect:
        query = value
        if "://" in value or value.startswith("/"):
            parsed = urlparse(value)
            query = parsed.query or parsed.fragment
        elif value.startswith("?"):
            query = value[1:]
        params = parse_qs(query, keep_blank_values=False)

        if "error" in params:
            description = (params.get("error_description") or params["error"])[0]
            raise OAuthError(f"Die Anmeldung wurde abgelehnt: {description}")

        codes = params.get("code")
        if not codes:
            raise OAuthError(
                "In der eingefuegten Adresse steckt kein 'code'-Parameter. Bitte die "
                "komplette Adresse aus der Adresszeile des Browsers kopieren, nachdem "
                "die Anmeldung durchgelaufen ist."
            )
        states = params.get("state")
        return codes[0], (states[0] if states else None)

    # Just the code itself.  Authorization codes are long and opaque; a short
    # value is almost certainly the user pasting the wrong thing.
    if len(value) < 12 or " " in value:
        raise OAuthError(
            "Das sieht nicht nach einem Anmelde-Code aus. Bitte die komplette Adresse "
            "aus der Adresszeile des Browsers einfuegen (sie beginnt mit "
            "http://localhost:1455/auth/callback?code=...)."
        )
    return value, None


# --------------------------------------------------------------------------- #
# Token handling
# --------------------------------------------------------------------------- #

def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT payload without verifying it.

    Verification would prove nothing useful here: the token arrived over TLS
    from the authorization server we just talked to, and it is only read for
    display and for the account id.  It is never trusted for authorisation.
    """
    if not token or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


@dataclass
class TokenSet:
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    account_id: str = ""
    account_label: str = ""
    plan_type: str = ""
    expires_at: dt.datetime | None = None
    raw_claims: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        now = dt.datetime.now(dt.timezone.utc)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.timezone.utc)
        return now >= (expires - REFRESH_MARGIN)


def _tokenset_from_payload(payload: dict[str, Any], previous: TokenSet | None = None) -> TokenSet:
    """Build a TokenSet from a token endpoint response."""
    tokens = TokenSet(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
        id_token=str(payload.get("id_token") or ""),
    )
    if not tokens.refresh_token and previous is not None:
        # Refresh responses may omit the refresh token; the old one stays valid.
        tokens.refresh_token = previous.refresh_token
    if not tokens.id_token and previous is not None:
        tokens.id_token = previous.id_token

    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        tokens.expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(expires_in))

    claims = decode_jwt_claims(tokens.id_token)
    tokens.raw_claims = claims
    auth = claims.get(AUTH_CLAIM) or {}
    if isinstance(auth, dict):
        tokens.account_id = str(
            auth.get("chatgpt_account_id") or auth.get("chatgpt_user_id") or ""
        )
        tokens.plan_type = str(auth.get("chatgpt_plan_type") or "")
    tokens.account_label = str(claims.get("email") or claims.get("preferred_username") or "")

    if not tokens.account_id and previous is not None:
        tokens.account_id = previous.account_id
    if not tokens.account_label and previous is not None:
        tokens.account_label = previous.account_label
    if not tokens.plan_type and previous is not None:
        tokens.plan_type = previous.plan_type
    return tokens


async def _post_token(body: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    if not HTTP_AVAILABLE:
        raise OAuthError("Das Python-Paket 'httpx' fehlt im Container.")
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            response = await client.post(
                TOKEN_URL,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
        except httpx.ConnectError as exc:
            raise OAuthError(
                "Keine Verbindung zu auth.openai.com - hat der Container Internetzugang?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise OAuthError("Zeitueberschreitung bei auth.openai.com.") from exc

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}

    if response.status_code != 200:
        message = ""
        if isinstance(payload, dict):
            message = str(
                payload.get("error_description") or payload.get("error") or ""
            )
        raise OAuthError(
            f"Der Anmelde-Server hat abgelehnt (HTTP {response.status_code})"
            + (f": {message}" if message else f": {response.text[:200]}")
        )
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise OAuthError("Der Anmelde-Server hat kein Zugangstoken geliefert.")
    return payload


async def exchange_code(code: str, verifier: str, redirect_uri: str = REDIRECT_URI) -> TokenSet:
    """Trade the authorization code for tokens."""
    payload = await _post_token({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    })
    tokens = _tokenset_from_payload(payload)
    if not tokens.refresh_token:
        log.warning("token response had no refresh token - sign-in will expire")
    return tokens


async def refresh_tokens(previous: TokenSet) -> TokenSet:
    """Renew an expiring access token."""
    if not previous.refresh_token:
        raise OAuthError("Kein Refresh-Token vorhanden - bitte erneut anmelden.")
    payload = await _post_token({
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": previous.refresh_token,
        "scope": SCOPES,
    })
    return _tokenset_from_payload(payload, previous=previous)


def tokens_from_auth_json(raw: str) -> TokenSet:
    """Import credentials from a ``~/.codex/auth.json`` the user pasted.

    The file has moved its keys around between CLI versions, so every plausible
    location is checked rather than assuming one layout.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise OAuthError("Das ist kein gueltiges JSON.") from exc
    if not isinstance(data, dict):
        raise OAuthError("Erwartet wird der Inhalt der Datei auth.json (ein JSON-Objekt).")

    # Current layout nests under "tokens"; older ones were flat.
    candidates: list[dict[str, Any]] = [data]
    for key in ("tokens", "token", "credentials", "openai"):
        nested = data.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    def pick(*names: str) -> str:
        for source in candidates:
            for name in names:
                value = source.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    tokens = TokenSet(
        access_token=pick("access_token", "accessToken"),
        refresh_token=pick("refresh_token", "refreshToken"),
        id_token=pick("id_token", "idToken"),
    )
    if not tokens.access_token and not tokens.refresh_token:
        raise OAuthError(
            "In der Datei stecken weder ein access_token noch ein refresh_token. "
            "Stammt sie wirklich von 'codex login'?"
        )

    claims = decode_jwt_claims(tokens.id_token or tokens.access_token)
    tokens.raw_claims = claims
    auth = claims.get(AUTH_CLAIM) or {}
    if isinstance(auth, dict):
        tokens.account_id = str(auth.get("chatgpt_account_id") or "")
        tokens.plan_type = str(auth.get("chatgpt_plan_type") or "")
    tokens.account_label = str(claims.get("email") or "")

    # Explicit account id in the file wins over anything decoded.
    explicit = pick("account_id", "accountId", "chatgpt_account_id")
    if explicit:
        tokens.account_id = explicit

    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        tokens.expires_at = dt.datetime.fromtimestamp(int(exp), dt.timezone.utc)
    return tokens
