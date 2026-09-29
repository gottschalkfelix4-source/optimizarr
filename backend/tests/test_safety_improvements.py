"""Regression tests for ownership, durable commits, restore and old DB upgrades."""
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.pool import StaticPool

from app import config, db
from app.main import app
from app.migrations import upgrade
from app.models import Base, LibraryPath, MediaFile, Job
from app.core import durable, output_files, trash, planner, maintenance, scanner
from app.core.ffmpeg import MediaInfo


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(scanner, "state", scanner.ScanState())
    yield TestClient(app)
    engine.dispose()


def converted(tmp_path, suffix="mkv"):
    source = tmp_path / f"Film.{suffix}"
    source.write_bytes(b"original version")
    out = tmp_path / "encoded.mkv"
    out.write_bytes(b"encoded")
    settings = config.AppSettings()
    settings.output.mode = "replace"
    settings.output.original_action = "trash"
    settings.output.set_permissions = False
    target = output_files._commit_output(str(source), str(out), planner.EncodePlan(container="mkv"), settings, MediaInfo(path=str(source)))
    item = trash.listing(14)["items"][0]
    return source, Path(target), item


def age(item):
    path = trash.record_path(item["id"])
    record = json.loads(path.read_text())
    record["trashed_at"] = 0
    path.write_text(json.dumps(record))


def test_purge_does_not_claim_foreign_date_directories(tmp_path):
    file = tmp_path / "2020-01-01" / "family.mkv"
    file.parent.mkdir()
    file.write_bytes(b"precious")
    os.utime(file, (0, 0))
    cfg = config.AppSettings()
    cfg.output.trash_dir = str(tmp_path)
    assert output_files.purge_trash(cfg) == 0
    assert file.read_bytes() == b"precious"


def test_only_recorded_unchanged_originals_expire(tmp_path):
    _, _, item = converted(tmp_path)
    age(item)
    assert trash.purge(14) == 1
    assert not Path(item["path"]).exists()


@pytest.mark.parametrize("change", ["edit", "symlink"])
def test_changed_trash_is_never_deleted(tmp_path, change):
    _, _, item = converted(tmp_path)
    age(item)
    path = Path(item["path"])
    if change == "edit":
        path.write_bytes(b"someone else's file")
    else:
        path.unlink()
        foreign = tmp_path / "foreign.mkv"
        foreign.write_bytes(b"precious")
        path.symlink_to(foreign)
    assert trash.purge(14) == 0
    assert path.exists()
    assert trash.listing(14)["items"][0]["conflict"]


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_journal_failure_prevents_any_media_mutation(tmp_path, monkeypatch, failure):
    source = tmp_path / "film.mkv"
    source.write_bytes(b"original")
    out = tmp_path / "out.mkv"
    out.write_bytes(b"encoded")
    if failure == "write":
        monkeypatch.setattr(durable, "write_json", Mock(side_effect=OSError("disk full")))
    else:
        monkeypatch.setattr(durable.os, "fsync", Mock(side_effect=OSError("I/O failure")))
    cfg = config.AppSettings()
    cfg.output.mode = "replace"
    with pytest.raises(OSError):
        output_files._commit_output(str(source), str(out), planner.EncodePlan(), cfg, MediaInfo(path=str(source)))
    assert source.read_bytes() == b"original"
    assert out.read_bytes() == b"encoded"
    assert not list(tmp_path.glob(".optimizarr-*"))


def test_journal_flushed_before_original_is_secured(tmp_path, monkeypatch):
    events = []
    sync, secure = durable.sync_dir, output_files._secure_original
    monkeypatch.setattr(durable, "sync_dir", lambda p: (events.append(str(p)), sync(p)))
    monkeypatch.setattr(output_files, "_secure_original", lambda *a: (events.append("secure"), secure(*a))[1])
    converted(tmp_path)
    assert str(output_files.COMMIT_JOURNAL_DIR) in events[:events.index("secure")]


@pytest.mark.parametrize("suffix", ["mkv", "avi"])
def test_restore_preserves_original_and_encoded_version(client, tmp_path, suffix):
    source, target, item = converted(tmp_path, suffix)
    with db.session_scope() as s:
        row = MediaFile(path=str(target), state="done", video_codec="av1", size=7, original_size=16)
        s.add(row)
        s.flush()
        row_id = row.id
    response = client.post(f'/api/trash/{item["id"]}/restore')
    assert response.status_code == 200, response.text
    assert source.read_bytes() == b"original version"
    assert Path(response.json()["kept_output"]).read_bytes() == b"encoded"
    assert not Path(item["path"]).exists()
    with db.session_scope() as s:
        row = s.get(MediaFile, row_id)
        assert row.path == str(source) and row.ignored
        assert row.converted_at is None and row.plan is None
    assert client.get("/api/trash").json()["items"] == []
    assert client.post(f'/api/trash/{item["id"]}/restore').status_code == 409


def test_restore_refuses_external_replacement(client, tmp_path):
    source, _, item = converted(tmp_path)
    source.write_bytes(b"upgraded release")
    response = client.post(f'/api/trash/{item["id"]}/restore')
    assert response.status_code == 409
    assert source.read_bytes() == b"upgraded release"
    assert Path(item["path"]).read_bytes() == b"original version"


def test_restore_refuses_queued_job(client, tmp_path):
    source, target, item = converted(tmp_path)
    with db.session_scope() as s:
        row = MediaFile(path=str(target), state="queued")
        s.add(row)
        s.flush()
        s.add(Job(file_id=row.id, state="queued"))
    assert client.post(f'/api/trash/{item["id"]}/restore').status_code == 409
    assert source.read_bytes() == b"encoded"


def test_maintenance_prevents_queue_claims():
    fn = Mock(return_value=[1])
    guarded = maintenance.claim_guard(fn)
    with maintenance.exclusive():
        assert guarded() == []
        fn.assert_not_called()
    assert guarded() == [1]


def test_old_database_quality_migration_is_repeatable():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        for table in ("jobs", "media_files", "learning_samples"):
            c.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, vmaf FLOAT)")
            c.exec_driver_sql(f"INSERT INTO {table} (vmaf) VALUES (94)")
    upgrade(engine)
    upgrade(engine)
    with engine.connect() as c:
        for table in ("jobs", "media_files", "learning_samples"):
            assert {"quality_metric", "quality_value"} <= {x["name"] for x in inspect(c).get_columns(table)}
            assert tuple(c.exec_driver_sql(f"SELECT vmaf, quality_metric, quality_value FROM {table}").one()) == (94, None, None)


def seed_movies(count=121):
    with db.session_scope() as s:
        lib = LibraryPath(path="/media/movies", name="Filme")
        s.add(lib)
        s.flush()
        for i in range(count):
            s.add(MediaFile(path=f"/media/movies/Film {i:03d} (2020)/Film.mkv", library_id=lib.id,
                            video_codec="h264", state="candidate", size=1000,
                            plan={"encoder": "libsvtav1"}, estimated_saving_bytes=300))


def test_movie_search_pagination_and_full_filter_bulk(client):
    seed_movies()
    first = client.get("/api/movies?page_size=50").json()
    second = client.get("/api/movies?page_size=50&page=2").json()
    assert first["total"] == 121 and len(first["items"]) == 50 and first["pages"] == 3
    assert not {x["key"] for x in first["items"]} & {x["key"] for x in second["items"]}
    found = client.get("/api/movies?search=Film%20120").json()
    assert found["total"] == 1 and found["items"][0]["title"] == "Film 120"
    assert client.get("/api/movies?page_size=201").status_code == 422
    result = client.post("/api/movies/enqueue?search=Film&force=false")
    assert result.status_code == 200, result.text
    assert result.json()["added"] == 121


def test_group_cache_reuses_rows_and_invalidates_after_commit(client):
    from sqlalchemy import event
    seed_movies(3)
    reads = []
    def capture(conn, cursor, statement, *args):
        if "FROM media_files" in statement and statement.lstrip().upper().startswith("SELECT"):
            reads.append(statement)
    event.listen(db.engine(), "before_cursor_execute", capture)
    try:
        client.get("/api/movies")
        assert len(reads) == 1
        client.get("/api/movies?search=Film&page=2")
        client.get("/api/series")
        assert len(reads) == 1
        with db.session_scope() as s:
            row = s.scalar(select(MediaFile).limit(1))
            row.video_codec = "av1"
        reads.clear()
        result = client.get("/api/movies?filter=complete").json()
        assert result["total"] == 1
        assert len(reads) == 1
    finally:
        event.remove(db.engine(), "before_cursor_execute", capture)


def test_series_search_and_pagination(client):
    with db.session_scope() as s:
        lib = LibraryPath(path="/media/series", name="Serien")
        s.add(lib)
        s.flush()
        for i in range(65):
            s.add(MediaFile(path=f"/media/series/Show {i:03d}/Season 01/S01E01.mkv", library_id=lib.id,
                            video_codec="h264", state="candidate", size=1000))
    result = client.get("/api/series?page=2&page_size=50").json()
    assert result["all_count"] == 65 and len(result["items"]) == 15
    assert client.get("/api/series?search=Show%20064").json()["total"] == 1


def test_real_vmaf_average_excludes_estimates_and_unknown_history(client):
    with db.session_scope() as s:
        for i, (metric, raw, scaled) in enumerate([("vmaf", 90., 90.), ("ssim", .99, 98.), (None, None, 100.)]):
            s.add(MediaFile(path=f"/film{i}.mkv", state="done", original_size=100, size=50,
                            measured_vmaf=scaled, quality_metric=metric, quality_value=raw))
    assert client.get("/api/stats").json()["realised"]["average_vmaf"] == 90.


def test_restore_can_finish_after_interrupted_bookkeeping(client, tmp_path, monkeypatch):
    source, _, item = converted(tmp_path)
    finish = trash.finish_restore
    monkeypatch.setattr(trash, "finish_restore", Mock(side_effect=OSError("disk unavailable")))
    assert client.post(f'/api/trash/{item["id"]}/restore').status_code == 409
    assert source.read_bytes() == b"original version"
    assert client.get("/api/trash").json()["items"][0]["conflict"] == ""
    monkeypatch.setattr(trash, "finish_restore", finish)
    assert client.post(f'/api/trash/{item["id"]}/restore').status_code == 200
    assert source.read_bytes() == b"original version"


def test_quality_metric_and_raw_value_survive_recording(client, tmp_path):
    from app.core import encoder
    from app.core.encode_types import EncodeOutcome
    with db.session_scope() as s:
        row = MediaFile(path=str(tmp_path / "film.mkv"), state="encoding")
        s.add(row)
        s.flush()
        job = Job(file_id=row.id, state="running")
        s.add(job)
        s.flush()
        file_id, job_id = row.id, job.id
    cfg = config.AppSettings()
    cfg.output.mode = "sidecar"
    encoder._record_success(job_id, file_id, EncodeOutcome(ok=True, input_size=100, output_size=50,
        vmaf=94, quality_metric="ssim", quality_value=.98), str(tmp_path / "film.av1.mkv"),
        planner.EncodePlan(), MediaInfo(path=str(tmp_path / "film.mkv")), cfg)
    data = client.get(f"/api/jobs/{job_id}").json()
    assert data["quality_metric"] == "ssim" and data["quality_value"] == .98 and data["vmaf"] == 94


def test_restored_file_stays_ignored_even_when_library_row_was_removed(client, tmp_path):
    source, _, item = converted(tmp_path)
    assert client.post(f'/api/trash/{item["id"]}/restore').status_code == 200
    with db.session_scope() as s:
        row = s.scalar(select(MediaFile).where(MediaFile.path == str(source)))
        assert row is not None and row.ignored and row.state == "ignored"


def test_enqueue_is_blocked_during_restoration():
    from app.core.worker import enqueue_files
    with maintenance.exclusive():
        count, reasons = enqueue_files([123])
    assert count == 0 and "Wiederherstellung" in reasons[0]


def test_movie_bulk_rejects_unknown_filter(client):
    seed_movies(2)
    assert client.post("/api/movies/enqueue?filter=typo").status_code == 422
    with db.session_scope() as s:
        assert s.scalar(select(Job.id).limit(1)) is None
