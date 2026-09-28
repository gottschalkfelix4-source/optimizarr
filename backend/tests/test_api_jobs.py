"""Jobs list filters/counts and the resolution classes of the statistics."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api.routes_jobs import resolution_class
from app.main import app
from app.models import Job, JobState, MediaFile

from conftest import CSRF_HEADERS, isolated_app_state


@pytest.fixture()
def client(tmp_path):
    with isolated_app_state(tmp_path):
        yield TestClient(app, headers=CSRF_HEADERS)


@pytest.mark.parametrize("w,h,label", [
    (3840, 2160, "2160p"), (3840, 1600, "2160p"), (4096, 1716, "2160p"), (0, 2160, "2160p"),
    (2560, 1440, "1440p"), (2560, 1080, "1440p"),
    (1920, 1080, "1080p"), (1920, 800, "1080p"), (1440, 1080, "1080p"), (0, 1080, "1080p"),
    (1280, 720, "720p"), (1280, 534, "720p"), (960, 720, "720p"),
    (720, 576, "SD"), (640, 480, "SD"), (0, 0, "SD"), (None, None, "SD"),
])
def test_resolution_classes(w, h, label):
    assert resolution_class(w, h) == label


def test_stats_bucket_scope_films_by_width(client):
    with db.session_scope() as s:
        s.add_all([
            MediaFile(path="/m/a.mkv", width=1920, height=800, size=10),
            MediaFile(path="/m/b.mkv", width=3840, height=1600, size=20),
            MediaFile(path="/m/c.mkv", width=720, height=576, size=5),
        ])
    body = client.get("/api/stats").json()
    buckets = {b["label"]: b for b in body["resolutions"]}
    assert buckets["1080p"]["count"] == 1 and buckets["1080p"]["size"] == 10
    assert buckets["2160p"]["count"] == 1
    assert buckets["SD"]["count"] == 1
    assert "720p" not in buckets


def _jobs(states: list[str]) -> None:
    with db.session_scope() as s:
        for i, state in enumerate(states):
            media = MediaFile(path=f"/m/{i}.mkv")
            s.add(media)
            s.flush()
            s.add(Job(file_id=media.id, state=state))


def test_jobs_filters_and_counts(client):
    _jobs(["queued", "running", "done", "done", "failed", "rejected", "cancelled"])

    r = client.get("/api/jobs", params={"state": "active", "limit": 1000})
    assert r.status_code == 200
    body = r.json()
    assert sorted(j["state"] for j in body["items"]) == ["queued", "running"]
    # Counts cover every job, whatever the filter - and every state appears.
    assert body["counts"] == {
        "queued": 1, "running": 1, "done": 2, "failed": 1, "cancelled": 1, "rejected": 1,
    }

    finished = client.get("/api/jobs", params={"state": "finished"}).json()["items"]
    assert sorted(j["state"] for j in finished) == ["cancelled", "done", "done", "failed", "rejected"]

    assert len(client.get("/api/jobs", params={"state": "done"}).json()["items"]) == 2
    assert len(client.get("/api/jobs", params={"limit": 2}).json()["items"]) == 2


def test_counts_are_zero_on_an_empty_queue(client):
    counts = client.get("/api/jobs").json()["counts"]
    assert counts == {s.value: 0 for s in JobState}


def test_jobs_rejects_unknown_filters_and_huge_limits(client):
    assert client.get("/api/jobs", params={"state": "bogus"}).status_code == 422
    assert client.get("/api/jobs", params={"limit": 1001}).status_code == 422


def test_start_now_needs_waiting_jobs_and_can_be_withdrawn(client, monkeypatch):
    from app.core import worker
    monkeypatch.setattr(worker, "queue_worker", worker.QueueWorker())

    assert client.post("/api/queue/start-now", json={"active": True}).status_code == 409

    _jobs(["queued"])
    r = client.post("/api/queue/start-now", json={"active": True})
    assert r.status_code == 200
    assert r.json()["active"] is True and r.json()["worker"]["schedule_override"] is True

    r = client.post("/api/queue/start-now", json={"active": False})
    assert r.json()["active"] is False
