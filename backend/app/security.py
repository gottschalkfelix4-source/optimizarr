"""Access control and input hardening.

* Password hashing (PBKDF2-SHA256 from hashlib, no extra dependency).
* An ASGI middleware for optional HTTP Basic auth, the CSRF header and the
  WebSocket origin check.  It is plain ASGI rather than ``BaseHTTPMiddleware``
  because the latter never sees WebSocket handshakes.
* Validators for settings that end up on a command line or in the filesystem
  (extra ffmpeg arguments, file mode, output directories).

Nothing in here imports ``config`` at module level - ``config`` imports this
module for its validators.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import secrets
import shlex
from collections import OrderedDict
from typing import Any, Callable
from urllib.parse import urlsplit

import anyio
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

PBKDF2_ALGORITHM = "pbkdf2_sha256"
# OWASP's 2023 recommendation for PBKDF2-HMAC-SHA256.
PBKDF2_ITERATIONS = 600_000
_HASH_RE = re.compile(r"^pbkdf2_sha256\$(\d{1,8})\$([0-9a-f]{16,128})\$([0-9a-f]{64})$")


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """``pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>``."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return f"{PBKDF2_ALGORITHM}${iterations}${salt}${digest.hex()}"


def is_password_hash(value: str) -> bool:
    """True for a value ``hash_password`` produced - such a value is stored as is."""
    return bool(_HASH_RE.match(value or ""))


def verify_password(password: str, stored: str) -> bool:
    match = _HASH_RE.match(stored or "")
    if not match:
        return False
    iterations = int(match.group(1))
    if iterations < 1:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), match.group(2).encode("ascii"), iterations
    )
    return hmac.compare_digest(digest.hex(), match.group(3))


def parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token:
        return None
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    username, sep, password = decoded.partition(":")
    if not sep:
        return None
    return username, password


class BasicAuthVerifier:
    """Checks Basic credentials against the stored hash.

    PBKDF2 with 600k rounds costs a noticeable fraction of a second, and the UI
    sends a request every few seconds - so successful checks are remembered,
    keyed by a digest of (stored hash, username, password).  A changed password
    changes the stored hash and with it every key.  Failures are never cached.
    """

    def __init__(self, size: int = 64) -> None:
        self._ok: OrderedDict[bytes, None] = OrderedDict()
        self._size = size

    @staticmethod
    def _key(stored: str, username: str, password: str) -> bytes:
        return hashlib.sha256(
            "\0".join((stored, username, password)).encode("utf-8")
        ).digest()

    def is_cached(self, stored: str, username: str, password: str) -> bool:
        key = self._key(stored, username, password)
        if key in self._ok:
            self._ok.move_to_end(key)
            return True
        return False

    def verify(self, username: str, password: str, expected_user: str, stored: str) -> bool:
        """Constant time with respect to which part is wrong: both are checked."""
        user_ok = hmac.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))
        pw_ok = verify_password(password, stored)
        if user_ok and pw_ok:
            key = self._key(stored, username, password)
            self._ok[key] = None
            self._ok.move_to_end(key)
            while len(self._ok) > self._size:
                self._ok.popitem(last=False)
            return True
        return False

    def clear(self) -> None:
        self._ok.clear()


# --------------------------------------------------------------------------- #
# Middleware: Basic auth, CSRF header, WebSocket origin
# --------------------------------------------------------------------------- #

CSRF_HEADER = "X-Optimizarr"
CSRF_VALUE = "1"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
AUTH_REALM = 'Basic realm="Optimizarr", charset="UTF-8"'


def _public(method: str, path: str) -> bool:
    """What stays reachable without credentials."""
    if method == "OPTIONS":  # CORS preflights never carry credentials
        return True
    return method in ("GET", "HEAD") and path == "/api/health"


def origin_allowed(origin: str | None, host: str | None, forwarded_host: str | None = None) -> bool:
    """A browser WebSocket must come from the page this server delivered.

    No Origin at all is a non-browser client (scripts, curl) - those cannot be
    tricked into connecting, and the auth check still applies to them.
    """
    if not origin:
        return True
    try:
        netloc = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    if not netloc:
        return False
    candidates = {host.strip().lower()} if host else set()
    # X-Forwarded-Host may carry a list when several proxies are chained.
    if forwarded_host:
        candidates |= {h.strip().lower() for h in forwarded_host.split(",") if h.strip()}
    return netloc in candidates


class SecurityMiddleware:
    """Enforces the rules above for HTTP requests and WebSocket handshakes.

    ``get_security`` returns the current ``SecuritySettings`` (auth_enabled,
    username, password hash); it is called per request so a change in the UI
    applies immediately.
    """

    def __init__(self, app: ASGIApp, get_security: Callable[[], Any]) -> None:
        self.app = app
        self.get_security = get_security
        self.verifier = BasicAuthVerifier()

    async def _authorized(self, headers: Headers) -> bool:
        try:
            sec = self.get_security()
        except Exception:  # pragma: no cover - settings must never lock everybody out
            log.exception("could not read the security settings")
            return True
        if not (sec.auth_enabled and sec.password):
            return True
        creds = parse_basic_auth(headers.get("authorization"))
        if creds is None:
            return False
        username, password = creds
        if self.verifier.is_cached(sec.password, username, password):
            # A cache hit still compares the user name in constant time.
            return hmac.compare_digest(username.encode("utf-8"), sec.username.encode("utf-8"))
        # PBKDF2 blocks for a while - keep it off the event loop.
        return await anyio.to_thread.run_sync(
            self.verifier.verify, username, password, sec.username, sec.password
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        path = scope.get("path", "")

        if kind == "websocket":
            if not origin_allowed(
                headers.get("origin"), headers.get("host"), headers.get("x-forwarded-host")
            ) or not await self._authorized(headers):
                await WebSocketClose(code=1008)(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if not _public(method, path) and not await self._authorized(headers):
            response = JSONResponse(
                {"detail": "Anmeldung erforderlich."},
                status_code=401,
                headers={"WWW-Authenticate": AUTH_REALM},
            )
            await response(scope, receive, send)
            return
        if (
            path.startswith("/api")
            and method not in SAFE_METHODS
            and headers.get(CSRF_HEADER.lower()) != CSRF_VALUE
        ):
            response = JSONResponse(
                {"detail": f"Anfrage abgelehnt: Header {CSRF_HEADER}: {CSRF_VALUE} fehlt."},
                status_code=403,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def reset_auth_requested() -> bool:
    """``OPTIMIZARR_RESET_AUTH=1``: the way back in after a forgotten password."""
    return os.environ.get("OPTIMIZARR_RESET_AUTH", "").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Filesystem paths
# --------------------------------------------------------------------------- #

# The container's own system directories.  Nothing may be written there, and a
# library below one of them would put the encoder to work on the OS.
SYSTEM_PATHS = (
    "/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib", "/run", "/boot", "/app",
)


_LIB_RE = re.compile(r"lib(32|64|x32|exec)?")


def is_system_path(path: str) -> bool:
    """``/`` itself, or anything inside a system directory (``/lib*`` included)."""
    norm = os.path.normpath(path)
    if norm in ("/", "//"):
        return True
    for root in SYSTEM_PATHS:
        if norm == root or norm.startswith(root + "/"):
            return True
    first = norm.lstrip("/").split("/", 1)[0]
    return bool(_LIB_RE.fullmatch(first))  # /lib32, /lib64, /libx32


def check_directory_setting(value: str, label: str) -> str:
    """Empty is allowed (means "default"); anything else must be a safe absolute path."""
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith("/"):
        raise ValueError(f"{label} muss ein absoluter Pfad sein (beginnend mit /).")
    norm = os.path.normpath(value)
    if is_system_path(norm) or is_system_path(os.path.realpath(norm)):
        raise ValueError(f"{label} darf nicht / oder ein Systemordner ({norm}) sein.")
    return norm


def check_file_mode(value: str) -> str:
    """Octal permission bits between 0600 and 0777 - no setuid/setgid/sticky."""
    raw = (value or "").strip()
    if not re.fullmatch(r"0?[0-7]{3}|0o[0-7]{3}", raw):
        raise ValueError("Dateirechte muessen oktal angegeben werden, z. B. 0664.")
    mode = int(raw.removeprefix("0o"), 8)
    if not 0o600 <= mode <= 0o777:
        raise ValueError("Dateirechte muessen zwischen 0600 und 0777 liegen.")
    return f"{mode:04o}"


# --------------------------------------------------------------------------- #
# Extra ffmpeg arguments
# --------------------------------------------------------------------------- #
#
# The field is appended to the output options just before the output file.
# Anything that is not an option is taken by ffmpeg as an additional output
# file, and several options read or write files on their own (filters such as
# movie=/subtitles=, -attach, -passlogfile, -f tee, input URLs with protocols).
# So instead of guessing what is dangerous, only options that tune the encode or
# the muxer are accepted, each with exactly one value:
#
# * rate control / quality: b, maxrate, minrate, bufsize, crf, qp, q, qscale,
#   global_quality, rc_mode, qmin, qmax, preset, tune, quality, compression_level
# * GOP / structure: g, keyint_min, bf, refs, idr_interval, tiles, tile_cols,
#   tile_rows, profile, level, pix_fmt
# * encoder specific: svtav1-params (SVT-AV1), look_ahead, look_ahead_depth,
#   extbrc, low_power, async_depth, adaptive_i, adaptive_b, b_strategy (QSV)
# * colour signalling: color_primaries, color_trc, colorspace, color_range
# * audio: ac, ar, vbr, application, frame_duration, cutoff
# * muxer / metadata: metadata, disposition, movflags, max_interleave_delta,
#   avoid_negative_ts, cues_to_front, reserve_index_space, threads
#
# Deliberately absent: -y/-n (overwrite), -f (output format, e.g. tee/hls
# writing elsewhere), -i (another input, any protocol), -vf/-af/-filter* (file
# reading/writing filters), -map*, -c/-codec (would bypass the planner), -attach,
# -passlogfile, -t/-to/-ss/-fs (truncate the output).

_EXTRA_OPTIONS = frozenset({
    "b", "maxrate", "minrate", "bufsize", "crf", "qp", "q", "qscale", "global_quality",
    "rc_mode", "qmin", "qmax", "preset", "tune", "quality", "compression_level",
    "g", "keyint_min", "bf", "refs", "idr_interval", "tiles", "tile_cols", "tile_rows",
    "profile", "level", "pix_fmt",
    "svtav1-params", "look_ahead", "look_ahead_depth", "extbrc", "low_power",
    "async_depth", "adaptive_i", "adaptive_b", "b_strategy",
    "color_primaries", "color_trc", "colorspace", "color_range",
    "ac", "ar", "vbr", "application", "frame_duration", "cutoff",
    "metadata", "disposition", "movflags", "max_interleave_delta", "avoid_negative_ts",
    "cues_to_front", "reserve_index_space", "threads",
})
# Free text is fine here: metadata is only written into the container.
_TEXT_OPTIONS = frozenset({"metadata"})
# SVT-AV1 keys that name files (first-pass statistics, grain tables, ROI maps).
_SVT_FILE_KEYS = ("file", "stat", "table", "pass", "roi")
_OPTION_RE = re.compile(r"^-([a-z0-9_-]+)((?::[a-z0-9]+)*)$", re.IGNORECASE)
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


def check_extra_ffmpeg_args(value: str) -> str:
    """Validate the "extra ffmpeg arguments" field; returns it stripped."""
    text = (value or "").strip()
    if not text:
        return ""
    if any(c in text for c in "\0\r\n"):
        raise ValueError("Zusaetzliche ffmpeg-Argumente duerfen keine Zeilenumbrueche enthalten.")
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f"Zusaetzliche ffmpeg-Argumente nicht lesbar: {exc}") from exc

    i = 0
    while i < len(tokens):
        token = tokens[i]
        match = _OPTION_RE.match(token)
        if not match:
            raise ValueError(
                f"'{token}' ist keine Option. Zusaetzliche Ausgabedateien oder Pfade "
                "sind nicht erlaubt."
            )
        name = match.group(1).lower()
        if name not in _EXTRA_OPTIONS:
            raise ValueError(
                f"Die Option '-{name}' ist nicht erlaubt. Erlaubt sind nur Encoder- und "
                "Muxer-Optionen wie -svtav1-params, -g, -maxrate oder -metadata."
            )
        if i + 1 >= len(tokens):
            raise ValueError(f"Der Option '{token}' fehlt ein Wert.")
        arg = tokens[i + 1]
        if arg.startswith("-") and not _NUMBER_RE.match(arg):
            raise ValueError(f"Der Option '{token}' fehlt ein Wert.")
        if name not in _TEXT_OPTIONS:
            if "/" in arg or "\\" in arg or "://" in arg:
                raise ValueError(f"Der Wert von '{token}' darf keinen Pfad oder keine URL enthalten.")
            if name == "svtav1-params":
                for part in arg.split(":"):
                    key = part.split("=", 1)[0].strip().lower()
                    if any(bad in key for bad in _SVT_FILE_KEYS):
                        raise ValueError(
                            f"Der SVT-AV1-Parameter '{key}' verweist auf eine Datei und "
                            "ist nicht erlaubt."
                        )
        i += 2
    return text
