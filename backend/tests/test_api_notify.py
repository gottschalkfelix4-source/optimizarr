"""Webhook notifications: what is sent when, and the test endpoint."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import SECRET_MASK, update_settings
from app.core import notify
from app.core.events import EventBus, bus
from app.main import app
from app.models import Job, MediaFile

from conftest import CSRF_HEADERS, isolated_app_state

URL = "https://hooks.example/abc/token"


@pytest.fixture()
def sent(monkeypatch):
    """Every request notify.send makes, answered with 204."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    real = httpx.AsyncClient

    class Recording(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(notify.httpx, "AsyncClient", Recording)
    return requests


@pytest.fixture()
def state(tmp_path):
    with isolated_app_state(tmp_path):
        yield


def _job(path="/media/Filme/Dune.mkv") -> tuple[int, int]:
    with db.session_scope() as s:
        media = MediaFile(path=path)
        s.add(media)
        s.flush()
        job = Job(file_id=media.id)
        s.add(job)
        s.flush()
        return job.id, media.id


def test_send_posts_the_documented_body(state, sent):
    ok, message = asyncio.run(notify.send("job.finished", "Titel", "Text", {"a": 1}, url=URL))
    assert ok, message
    body = json.loads(sent[0].content)
    assert body["event"] == "job.finished"
    assert body["title"] == "Titel" and body["message"] == "Text" and body["data"] == {"a": 1}
    assert body["content"] == "Titel: Text"   # Discord
    assert str(sent[0].url) == URL


def test_send_reports_errors_without_raising(state, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("down")

    real = httpx.AsyncClient

    class Failing(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(notify.httpx, "AsyncClient", Failing)
    ok, message = asyncio.run(notify.send("test", "t", "m", {}, url=URL))
    assert not ok and "nicht erreichbar" in message
    ok, message = asyncio.run(notify.send("test", "t", "m", {}, url="ftp://x"))
    assert not ok


@pytest.mark.parametrize("flags,state_,expected", [
    ({"notify_on_job_done": True}, "done", "Konvertierung abgeschlossen: Dune.mkv"),
    ({"notify_on_job_done": False}, "done", None),
    ({"notify_on_job_failed": True}, "failed", "Konvertierung fehlgeschlagen: Dune.mkv"),
    ({"notify_on_job_failed": True}, "rejected", "Ergebnis verworfen: Dune.mkv"),
    ({"notify_on_job_failed": False}, "failed", None),
    ({"notify_on_job_done": True, "notify_on_job_failed": True}, "cancelled", None),
])
def test_job_events(state, flags, state_, expected):
    update_settings({"notifications": {"webhook_url": URL, "notify_on_job_done": False,
                                       "notify_on_job_failed": False, **flags}})
    job_id, file_id = _job()
    built = asyncio.run(notify.build({
        "type": "job.finished", "data": {"job_id": job_id, "state": state_, "message": "x"},
    }))
    assert (built[1] if built else None) == expected


def test_scan_event_and_no_url(state):
    update_settings({"notifications": {"webhook_url": URL, "notify_on_scan_done": True}})
    built = asyncio.run(notify.build({
        "type": "scan.finished", "data": {"analyzed": 12, "candidates": 3, "error": ""},
    }))
    assert built[1] == "Scan abgeschlossen" and "3 Kandidaten" in built[2]
    update_settings({"notifications": {"webhook_url": ""}})
    assert asyncio.run(notify.build({"type": "scan.finished", "data": {}})) is None


def test_run_forwards_bus_events(state, sent):
    update_settings({"notifications": {"webhook_url": URL, "notify_on_job_failed": True}})
    job_id, _ = _job()

    async def scenario():
        local = EventBus()
        local.bind_loop(asyncio.get_running_loop())
        task = asyncio.create_task(notify.run(local))
        await asyncio.sleep(0)
        local.publish("job.progress", {"job_id": job_id})
        local.publish("job.finished", {"job_id": job_id, "state": "failed", "message": "kaputt"})
        for _ in range(50):
            if sent:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert local.subscriber_count == 0

    asyncio.run(scenario())
    assert len(sent) == 1
    body = json.loads(sent[0].content)
    assert body["event"] == "job.finished" and body["message"] == "kaputt"
    assert body["data"]["name"] == "Dune.mkv"


def test_lifespan_subscribes_the_notifier(tmp_path):
    with isolated_app_state(tmp_path):
        before = bus.subscriber_count
        with TestClient(app):
            assert bus.subscriber_count == before + 1
        assert bus.subscriber_count == before


def test_test_endpoint(tmp_path, sent):
    with isolated_app_state(tmp_path):
        client = TestClient(app, headers=CSRF_HEADERS)
        r = client.post("/api/notifications/test", json={})
        assert r.status_code == 422

        update_settings({"notifications": {"webhook_url": URL}})
        assert client.get("/api/settings").json()["notifications"]["webhook_url"] == SECRET_MASK
        # The masked value from the settings form means "the stored URL".
        r = client.post("/api/notifications/test", json={"webhook_url": SECRET_MASK})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert str(sent[-1].url) == URL
        # An unsaved URL from the form can be tried out directly.
        r = client.post("/api/notifications/test", json={"webhook_url": "https://other.example/x"})
        assert r.json()["ok"] is True
        assert str(sent[-1].url) == "https://other.example/x"
        assert json.loads(sent[-1].content)["event"] == "test"
