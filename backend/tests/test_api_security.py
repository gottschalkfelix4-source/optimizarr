"""Secrets, password hashing, Basic auth, CSRF header and WebSocket origin."""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import config, security
from app.config import SECRET_MASK, AppSettings, load_settings, update_settings
from app.main import app

from conftest import CSRF_HEADERS, isolated_app_state

PASSWORD = "richtig-geheim"


@pytest.fixture()
def client(tmp_path):
    # No lifespan: the middleware and the routes only need the database.
    with isolated_app_state(tmp_path):
        yield TestClient(app, headers=CSRF_HEADERS)


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def enable_auth(user: str = "admin", password: str = PASSWORD) -> None:
    update_settings({"security": {"auth_enabled": True, "username": user, "password": password}})


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #

def test_password_hash_format_and_verification():
    stored = security.hash_password("pw")
    algo, iterations, salt, digest = stored.split("$")
    assert algo == "pbkdf2_sha256"
    assert int(iterations) >= 600_000
    assert len(salt) >= 32 and len(digest) == 64
    assert security.verify_password("pw", stored)
    assert not security.verify_password("PW", stored)
    # A random salt: the same password never gives the same hash twice.
    assert security.hash_password("pw") != stored


def test_saving_a_password_stores_only_the_hash(client):
    r = client.put("/api/settings", json={"security": {"password": PASSWORD}})
    assert r.status_code == 200
    assert r.json()["security"]["password"] == SECRET_MASK
    stored = load_settings(force=True).security.password
    assert security.is_password_hash(stored)
    assert PASSWORD not in stored
    assert security.verify_password(PASSWORD, stored)


def test_a_stored_hash_is_not_hashed_again(client):
    stored = security.hash_password("pw")
    settings = AppSettings.model_validate({"security": {"password": stored}})
    assert settings.security.password == stored
    # Saving the settings again (any unrelated change) keeps the hash as is.
    config.save_settings(settings)
    assert config.load_settings(force=True).security.password == stored


# --------------------------------------------------------------------------- #
# Secrets in API answers
# --------------------------------------------------------------------------- #

def _set_secrets() -> None:
    update_settings({
        "advisor": {"api_key": "sk-ant-geheim", "openai_api_key": "sk-openai-geheim"},
        "notifications": {"webhook_url": "https://discord.example/api/webhooks/1/token"},
    })


def _assert_no_secret(text: str) -> None:
    for secret in ("sk-ant-geheim", "sk-openai-geheim", "webhooks/1/token", "pbkdf2_sha256$6"):
        assert secret not in text


def test_get_settings_masks_secrets(client):
    _set_secrets()
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["advisor"]["api_key"] == SECRET_MASK
    assert body["advisor"]["openai_api_key"] == SECRET_MASK
    assert body["notifications"]["webhook_url"] == SECRET_MASK
    # Not set -> empty, so the UI can tell the two apart.
    assert body["security"]["password"] == ""
    _assert_no_secret(r.text)


def test_every_settings_answer_is_masked(client):
    _set_secrets()
    enable_auth()
    auth = basic("admin", PASSWORD)
    for r in (
        client.put("/api/settings", json={"encoding": {"crf": 28}}, headers=auth),
        client.post("/api/settings/profile/space", headers=auth),
        client.post("/api/settings/reset", headers=auth),
        client.get("/api/settings/schema", headers=auth),
    ):
        assert r.status_code == 200, r.text
        _assert_no_secret(r.text)


def test_put_with_mask_keeps_and_empty_clears(client):
    _set_secrets()
    r = client.put("/api/settings", json={"advisor": {"api_key": SECRET_MASK, "model": "x"}})
    assert r.status_code == 200
    assert load_settings().advisor.api_key == "sk-ant-geheim"
    assert load_settings().advisor.model == "x"

    client.put("/api/settings", json={"advisor": {"api_key": ""}})
    assert load_settings().advisor.api_key == ""
    assert client.get("/api/settings").json()["advisor"]["api_key"] == ""


def test_schema_names_the_secret_fields(client):
    body = client.get("/api/settings/schema").json()
    assert "advisor.api_key" in body["secret_fields"]
    assert "security.password" in body["secret_fields"]
    assert body["secret_mask"] == SECRET_MASK


def test_reset_keeps_the_login(client):
    enable_auth()
    r = client.post("/api/settings/reset", headers=basic("admin", PASSWORD))
    assert r.status_code == 200
    assert load_settings().security.auth_enabled


# --------------------------------------------------------------------------- #
# Basic auth
# --------------------------------------------------------------------------- #

def test_enabling_auth_without_password_is_rejected(client):
    r = client.put("/api/settings", json={"security": {"auth_enabled": True}})
    assert r.status_code == 422
    assert "Passwort" in r.json()["detail"]
    assert not load_settings().security.auth_enabled


def test_auth_is_off_by_default(client):
    assert client.get("/api/stats").status_code == 200


def test_auth_protects_api_ui_and_leaves_health_open(client):
    enable_auth()
    r = client.get("/api/stats")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"].startswith("Basic")
    assert client.get("/").status_code == 401          # the web UI itself
    assert client.get("/api/docs").status_code == 401
    assert client.get("/api/health").status_code == 200  # Docker healthcheck

    assert client.get("/api/stats", headers=basic("admin", "falsch")).status_code == 401
    assert client.get("/api/stats", headers=basic("root", PASSWORD)).status_code == 401
    assert client.get("/api/stats", headers=basic("admin", PASSWORD)).status_code == 200
    # Second time from the cache - still correct, still checks the user name.
    assert client.get("/api/stats", headers=basic("admin", PASSWORD)).status_code == 200
    assert client.get("/api/stats", headers=basic("Admin", PASSWORD)).status_code == 401


def test_a_new_password_invalidates_the_old_one(client):
    enable_auth()
    assert client.get("/api/stats", headers=basic("admin", PASSWORD)).status_code == 200
    r = client.put(
        "/api/settings", json={"security": {"password": "neu"}}, headers=basic("admin", PASSWORD)
    )
    assert r.status_code == 200
    assert client.get("/api/stats", headers=basic("admin", PASSWORD)).status_code == 401
    assert client.get("/api/stats", headers=basic("admin", "neu")).status_code == 200


def test_malformed_authorization_headers_are_refused(client):
    enable_auth()
    for header in ("Basic", "Basic !!!", "Bearer abc", "Basic " + base64.b64encode(b"nocolon").decode()):
        assert client.get("/api/stats", headers={"Authorization": header}).status_code == 401


def test_websocket_requires_auth(client):
    enable_auth()
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/ws") as ws:
            ws.receive_text()
    assert exc.value.code == 1008
    with client.websocket_connect("/api/ws", headers=basic("admin", PASSWORD)) as ws:
        assert '"hello"' in ws.receive_text()


def test_reset_auth_env_switches_login_off(tmp_path, monkeypatch):
    with isolated_app_state(tmp_path):
        enable_auth()
        monkeypatch.setenv("OPTIMIZARR_RESET_AUTH", "1")
        with TestClient(app) as c:
            assert c.get("/api/stats").status_code == 200
        assert not load_settings(force=True).security.auth_enabled
        # The password stays - switching the login back on needs no new one.
        assert load_settings().security.password


# --------------------------------------------------------------------------- #
# CSRF header and WebSocket origin
# --------------------------------------------------------------------------- #

def test_writes_without_the_csrf_header_are_forbidden(client):
    bare = TestClient(app)
    bare.headers.pop("X-Optimizarr")
    r = bare.put("/api/settings", json={"encoding": {"crf": 20}})
    assert r.status_code == 403
    assert "X-Optimizarr" in r.json()["detail"]
    assert load_settings().encoding.crf != 20
    assert bare.post("/api/scan", json={}).status_code == 403
    assert bare.delete("/api/jobs/finished").status_code == 403
    assert bare.post("/api/scan", json={}, headers={"X-Optimizarr": "0"}).status_code == 403
    # Reading needs no header.
    assert bare.get("/api/settings").status_code == 200
    # With it, the same write goes through.
    assert bare.put("/api/settings", json={"encoding": {"crf": 20}},
                    headers=CSRF_HEADERS).status_code == 200


def test_websocket_rejects_foreign_origins(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/ws", headers={"Origin": "http://evil.example"}) as ws:
            ws.receive_text()
    assert exc.value.code == 1008
    # TestClient talks to host "testserver".
    with client.websocket_connect("/api/ws", headers={"Origin": "http://testserver"}) as ws:
        assert '"hello"' in ws.receive_text()
    with client.websocket_connect("/api/ws") as ws:  # no Origin: not a browser
        assert '"hello"' in ws.receive_text()


@pytest.mark.parametrize("origin,host,forwarded,ok", [
    ("http://tower:8474", "tower:8474", None, True),
    ("http://TOWER:8474", "tower:8474", None, True),
    ("http://tower:8474", "tower:8475", None, False),
    ("https://optimizarr.example", "172.17.0.2:8474", "optimizarr.example", True),
    ("null", "tower:8474", None, False),
    (None, "tower:8474", None, True),
])
def test_origin_rule(origin, host, forwarded, ok):
    assert security.origin_allowed(origin, host, forwarded) is ok


# --------------------------------------------------------------------------- #
# Error handler
# --------------------------------------------------------------------------- #

def test_unhandled_errors_do_not_leak_details(client, monkeypatch):
    from app.core import worker

    def boom():
        raise RuntimeError("/config/optimizarr.db kaputt")

    monkeypatch.setattr(worker.queue_worker, "status", boom)
    quiet = TestClient(app, raise_server_exceptions=False)
    r = quiet.get("/api/jobs")
    assert r.status_code == 500
    assert "optimizarr.db" not in r.text and "RuntimeError" not in r.text
    assert "Interner Fehler" in r.json()["detail"]
