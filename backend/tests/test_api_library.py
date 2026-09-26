"""Library API: ignoring files, library paths, the folder picker, search."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import routes_library
from app.core import scanner, worker
from app.main import app
from app.models import FileState, Job, JobState, LibraryPath, MediaFile

from conftest import CSRF_HEADERS, isolated_app_state


@pytest.fixture()
def client(tmp_path):
    with isolated_app_state(tmp_path):
        yield TestClient(app, headers=CSRF_HEADERS)


def add_file(path="/media/f/film.mkv", state=FileState.CANDIDATE.value, **kw) -> int:
    values = dict(video_codec="h264", size=1000)
    values.update(kw)
    with db.session_scope() as s:
        row = MediaFile(path=path, state=state, **values)
        s.add(row)
        s.flush()
        return row.id


def file_state(file_id: int) -> MediaFile:
    with db.session_scope() as s:
        return s.get(MediaFile, file_id)


# --------------------------------------------------------------------------- #
# Ignoring
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("state", ["queued", "encoding", "analyzing", "done"])
def test_busy_or_converted_files_cannot_be_ignored(client, state):
    file_id = add_file(state=state)
    r = client.post(f"/api/files/{file_id}/ignore")
    assert r.status_code == 409
    assert "Job" in r.json()["detail"] or "konvertiert" in r.json()["detail"]
    assert file_state(file_id).state == state
    assert not file_state(file_id).ignored


@pytest.mark.parametrize("state", ["new", "probed", "candidate", "skipped", "failed", "missing"])
def test_idle_files_can_be_ignored(client, state):
    file_id = add_file(state=state)
    r = client.post(f"/api/files/{file_id}/ignore")
    assert r.status_code == 200
    assert r.json()["state"] == "ignored" and r.json()["ignored"] is True


@pytest.mark.parametrize("kw,expected", [
    ({"video_codec": "av1", "converted_at": dt.datetime(2026, 9, 1)}, "done"),
    ({"video_codec": "av1"}, "probed"),
    ({"video_codec": "h264"}, "probed"),
    ({"video_codec": ""}, "new"),
])
def test_unignoring_restores_a_sensible_state(client, kw, expected):
    file_id = add_file(state="ignored", ignored=True, **kw)
    r = client.post(f"/api/files/{file_id}/ignore", params={"ignored": False})
    assert r.status_code == 200
    assert r.json()["state"] == expected
    assert r.json()["ignored"] is False


def test_bulk_unignore_resets_the_state(client):
    converted = add_file("/media/f/a.mkv", state="ignored", ignored=True,
                         video_codec="av1", converted_at=dt.datetime(2026, 9, 1))
    probed = add_file("/media/f/b.mkv", state="ignored", ignored=True, video_codec="hevc")
    r = client.post("/api/files/bulk/ignore", params={"ignored": False},
                    json={"file_ids": [converted, probed]})
    assert r.status_code == 200 and r.json()["updated"] == 2
    assert file_state(converted).state == "done"
    assert file_state(probed).state == "probed"
    assert not file_state(probed).ignored


def test_bulk_ignore_with_a_busy_file_changes_nothing(client):
    idle = add_file("/media/f/a.mkv", state="candidate")
    busy = add_file("/media/f/b.mkv", state="encoding")
    r = client.post("/api/files/bulk/ignore", json={"file_ids": [idle, busy]})
    assert r.status_code == 409
    assert file_state(idle).state == "candidate" and not file_state(idle).ignored


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def test_search_treats_like_wildcards_literally(client):
    add_file("/media/f/Show S01_E01.mkv")
    add_file("/media/f/Show S01xE01.mkv")
    add_file("/media/f/100% Wolf.mkv")
    add_file("/media/f/1000 Wolf.mkv")

    names = lambda q: sorted(i["name"] for i in client.get("/api/files", params={"search": q, "state": "all"}).json()["items"])  # noqa: E731
    assert names("s01_e01") == ["Show S01_E01.mkv"]
    assert names("100%") == ["100% Wolf.mkv"]
    assert names("show") == ["Show S01_E01.mkv", "Show S01xE01.mkv"]


# --------------------------------------------------------------------------- #
# Library paths
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", ["/", "/etc", "/proc", "/sys/kernel", "/dev", "/usr/share",
                                  "/bin", "/lib", "/lib64", "/run", "/boot", "/app", "media"])
def test_system_paths_cannot_become_libraries(client, path):
    r = client.post("/api/library/paths", json={"path": path})
    assert r.status_code == 422, path


def test_a_real_folder_can_be_added_without_profile(client, tmp_path):
    folder = tmp_path / "Filme"
    folder.mkdir()
    r = client.post("/api/library/paths", json={"path": str(folder) + "/"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(folder) and body["name"] == "Filme"
    assert "profile" not in body


def test_patch_validates_its_payload(client, tmp_path):
    with db.session_scope() as s:
        lib = LibraryPath(path=str(tmp_path), name="alt", enabled=True)
        s.add(lib)
        s.flush()
        lib_id = lib.id
    assert client.patch(f"/api/library/paths/{lib_id}", json={"enabled": "vielleicht"}).status_code == 422
    assert client.patch(f"/api/library/paths/{lib_id}", json={"name": "x" * 300}).status_code == 422
    r = client.patch(f"/api/library/paths/{lib_id}", json={"enabled": False, "name": "neu"})
    assert r.status_code == 200
    assert r.json()["enabled"] is False and r.json()["name"] == "neu"
    assert client.patch("/api/library/paths/999", json={"enabled": True}).status_code == 404


def _library_with_jobs(root: str) -> tuple[int, int, int, int, int]:
    with db.session_scope() as s:
        lib = LibraryPath(path=root, name="lib", enabled=True)
        s.add(lib)
        s.flush()
        running_file = MediaFile(path=f"{root}/a.mkv", library_id=lib.id, state="encoding")
        queued_file = MediaFile(path=f"{root}/b.mkv", library_id=lib.id, state="queued")
        s.add_all([running_file, queued_file])
        s.flush()
        running = Job(file_id=running_file.id, state=JobState.RUNNING.value)
        queued = Job(file_id=queued_file.id, state=JobState.QUEUED.value,
                     plan={"restore_state": "skipped"})
        s.add_all([running, queued])
        s.flush()
        return lib.id, running.id, queued.id, running_file.id, queued_file.id


@pytest.mark.parametrize("keep_files", [False, True])
def test_deleting_a_library_cancels_and_removes_its_jobs(client, tmp_path, monkeypatch, keep_files):
    lib_id, running_id, queued_id, running_file, queued_file = _library_with_jobs(str(tmp_path))
    cancelled: list[int] = []
    monkeypatch.setattr(worker.queue_worker, "cancel_job", lambda job_id: cancelled.append(job_id) or True)

    r = client.delete(f"/api/library/paths/{lib_id}", params={"keep_files": keep_files})
    assert r.status_code == 200
    assert cancelled == [running_id]
    assert r.json()["jobs_cancelled"] == 1 and r.json()["jobs_removed"] == 1
    with db.session_scope() as s:
        assert s.get(LibraryPath, lib_id) is None
        assert s.get(Job, queued_id) is None
        if keep_files:
            restored = s.get(MediaFile, queued_file)
            assert restored.library_id is None
            # planner.RESTORE_STATE of a forced job, else candidate
            assert restored.state in ("skipped", "candidate")
            assert s.get(MediaFile, running_file).state == "candidate"
        else:
            assert s.query(MediaFile).count() == 0


# --------------------------------------------------------------------------- #
# Folder picker
# --------------------------------------------------------------------------- #

def test_browse_answers_404_for_a_missing_folder(client, tmp_path):
    r = client.get("/api/library/browse", params={"path": str(tmp_path / "gibtsnicht")})
    assert r.status_code == 404
    assert "existiert" in r.json()["detail"]


def test_browse_lists_subfolders(client, tmp_path):
    (tmp_path / "Serien").mkdir()
    (tmp_path / ".versteckt").mkdir()
    (tmp_path / "datei.txt").write_text("x")
    r = client.get("/api/library/browse", params={"path": str(tmp_path)})
    assert r.status_code == 200
    assert [e["name"] for e in r.json()["entries"]] == ["Serien"]


# --------------------------------------------------------------------------- #
# Scans started from the API
# --------------------------------------------------------------------------- #

def test_started_scan_task_is_referenced(tmp_path, monkeypatch):
    with isolated_app_state(tmp_path):
        with db.session_scope() as s:
            s.add(LibraryPath(path=str(tmp_path), name="lib", enabled=True))
        release = None
        started = []

        async def fake_scan(**kw):
            nonlocal release
            release = asyncio.Event()
            started.append(kw)
            await release.wait()

        monkeypatch.setattr(scanner, "run_scan", fake_scan)
        with TestClient(app) as c:
            r = c.post("/api/scan", json={})
            assert r.status_code == 200
            assert started and started[0]["trigger"] == "manual"
            assert len(routes_library._scan_tasks) == 1
            task = next(iter(routes_library._scan_tasks))
            c.portal.call(release.set)
            c.portal.call(asyncio.sleep, 0.05)
            assert task.done()
            assert not routes_library._scan_tasks


def test_async_file_routes_still_answer_404(client):
    assert client.post("/api/files/4242/analyze").status_code == 404
    assert client.post("/api/files/4242/dry-run").status_code == 404
