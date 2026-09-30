"""User-flow and crash-injection regressions from PROJEKTREVIEW B1-B8."""
import asyncio
import datetime as dt
import json
import os
import threading
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from app import config, db
from app.main import app
from app.core import analyzer, background, durable, encoder, ffmpeg, group_cache, output_files, output_validation, planner, quality, scanner, scratch, worker
from app.models import HistoryEntry, Job, LearningSample, MediaFile, ScanRun, Setting
from conftest import isolated_app_state


@pytest.fixture
def client(tmp_path):
    with isolated_app_state(tmp_path):
        yield TestClient(app)


def media(tmp_path, state="candidate", ignored=False, codec="hevc"):
    path = tmp_path / f"film-{state}.mkv"
    path.write_bytes(b"original" * 1000)
    with db.session_scope() as s:
        row = MediaFile(path=str(path), state=state, ignored=ignored, size=8000,
                        video_codec=codec, plan=planner.EncodePlan().to_dict(), original_size=12000,
                        estimated_saving_bytes=4000)
        s.add(row)
        s.flush()
        return row.id, path


@pytest.mark.parametrize("mode", ["sidecar", "separate_dir"])
def test_output_alias_never_overwrites_original(client, tmp_path, mode):
    _, source = media(tmp_path)
    cfg = config.AppSettings()
    cfg.output.mode = mode
    cfg.output.set_permissions = False
    if mode == "sidecar":
        cfg.output.sidecar_suffix = ""
    else:
        cfg.output.output_dir = str(tmp_path)
    out = tmp_path / "temp.mkv"
    out.write_bytes(b"encoded")
    with pytest.raises(ValueError):
        output_files._commit_output(str(source), str(out), planner.EncodePlan(), cfg, ffmpeg.MediaInfo(path=str(source)))
    assert source.read_bytes() == b"original" * 1000


@pytest.mark.parametrize("kind", ["ordinary", "hardlink", "symlink"])
def test_sidecar_never_claims_an_existing_target(client, tmp_path, kind):
    _, source = media(tmp_path)
    target = source.with_name(source.stem + ".av1.mkv")
    if kind == "hardlink":
        os.link(source, target)
    elif kind == "symlink":
        target.symlink_to(source)
    else:
        target.write_bytes(b"foreign")
    before = target.read_bytes()
    cfg = config.AppSettings()
    cfg.output.mode = "sidecar"
    out = tmp_path / "temp.mkv"
    out.write_bytes(b"encoded")
    with pytest.raises((ValueError, FileExistsError)):
        output_files._commit_output(str(source), str(out), planner.EncodePlan(), cfg, ffmpeg.MediaInfo(path=str(source)))
    assert target.read_bytes() == before
    assert source.read_bytes() == b"original" * 1000


def test_failed_swap_journal_preserves_later_foreign_target(client, tmp_path, monkeypatch):
    source = tmp_path / "film.avi"
    source.write_bytes(b"original")
    out = tmp_path / "temp.mkv"
    out.write_bytes(b"encoded")
    cfg = config.AppSettings()
    cfg.output.original_action = "delete"
    monkeypatch.setattr(output_files, "_publish_new", lambda *a: (_ for _ in ()).throw(OSError("no space")))
    with pytest.raises(OSError):
        output_files._commit_output(str(source), str(out), planner.EncodePlan(), cfg, ffmpeg.MediaInfo(path=str(source)))
    target = source.with_suffix(".mkv")
    target.write_bytes(b"foreign created after failure")
    output_files._journal_write({"source": str(source), "target": str(target), "staging": str(tmp_path / ".optimizarr-staging-1-deadbeef-film.mkv"), "replace": True})
    assert output_files.recover_interrupted_commits() == 1
    assert target.read_bytes() == b"foreign created after failure"
    assert source.read_bytes() == b"original"


def test_settings_read_failure_keeps_auth_and_fails_closed(client, monkeypatch):
    config.update_settings({"security": {"auth_enabled": True, "password": "secret"}})
    valid = config.load_settings()
    monkeypatch.setattr(db, "session_scope", lambda: (_ for _ in ()).throw(OSError("unavailable")))
    assert config.load_settings(force=True) is valid
    assert client.get("/api/settings").status_code == 401
    monkeypatch.setattr(config, "_cache", None)
    assert client.get("/api/settings").status_code == 401


def test_corrupt_stored_auth_never_becomes_disabled(client):
    with db.session_scope() as s:
        s.get(Setting, "security").value = {"auth_enabled": True, "password": "", "username": "admin"}
    config.invalidate_cache()
    assert client.get("/api/settings").status_code == 401


def test_first_scan_due_does_not_slide(client):
    scheduler = worker.Scheduler()
    cfg = config.AppSettings()
    first = scheduler._compute_next(cfg)
    assert scheduler._compute_next(cfg) == first
    scheduler._first_scan_base -= dt.timedelta(hours=cfg.library.scan_interval_hours + 1)
    assert scheduler._compute_next(cfg) < dt.datetime.now(dt.timezone.utc)


@pytest.mark.parametrize("days", [[], [-1], [7]])
def test_invalid_schedule_is_rejected(client, days):
    assert client.put("/api/settings", json={"queue": {"schedule_days": days}}).status_code == 422


def test_schedule_days_are_normalized(client):
    response = client.put("/api/settings", json={"queue": {"schedule_days": [5, 1, 5]}})
    assert response.status_code == 200
    assert config.load_settings().queue.schedule_days == [1, 5]


def test_bulk_enqueue_requires_explicit_force_and_protects_completed(client, tmp_path):
    ids = [media(tmp_path, state, state == "ignored")[0] for state in ("candidate", "skipped", "ignored", "done")]
    response = client.post("/api/jobs", json={"file_ids": ids}).json()
    assert response["added"] == 1
    forced = client.post("/api/jobs", json={"file_ids": ids, "force": True}).json()
    assert forced["added"] == 2
    with db.session_scope() as s:
        assert s.get(MediaFile, ids[-1]).state == "done"
        assert s.get(MediaFile, ids[2]).ignored


@pytest.mark.parametrize("state", ["done", "ignored"])
def test_manual_analysis_preserves_terminal_state_and_saving(client, tmp_path, monkeypatch, state):
    file_id, path = media(tmp_path, state, state == "ignored", "av1")
    before = client.get("/api/stats").json()
    monkeypatch.setattr(ffmpeg, "probe", AsyncMock(return_value=ffmpeg.MediaInfo(path=str(path), video_codec="av1")))
    monkeypatch.setattr(scanner.hwaccel, "cached", lambda: SimpleNamespace())
    monkeypatch.setattr(analyzer, "analyze", AsyncMock(return_value=analyzer.AnalysisResult(decision="skip", reason="Bereits AV1")))
    response = client.post(f"/api/files/{file_id}/analyze?depth=quick")
    assert response.status_code == 200
    assert response.json()["state"] == state
    after = client.get("/api/stats").json()
    assert after == before


def test_unexpected_probe_failure_marks_scan_failed(client, tmp_path, monkeypatch):
    file_id, _ = media(tmp_path)
    monkeypatch.setattr(scanner.hwaccel, "cached", lambda: SimpleNamespace())
    monkeypatch.setattr(ffmpeg, "probe", AsyncMock(side_effect=RuntimeError("database unavailable")))
    result = asyncio.run(scanner.run_scan(analyze_only_ids=[file_id]))
    assert not result["ok"] and "database unavailable" in result["error"]
    with db.session_scope() as s:
        assert s.get(ScanRun, result["run_id"]).state == "failed"


@pytest.mark.parametrize("phase", ["prepared", "staged", "original_secured", "published", "filesystem_done"])
def test_crash_at_each_commit_phase_preserves_data_or_reconciles(client, tmp_path, monkeypatch, phase):
    file_id, source = media(tmp_path)
    info = ffmpeg.MediaInfo(path=str(source), size=source.stat().st_size, duration=10, video_codec="hevc")
    plan = planner.EncodePlan()
    cfg = config.AppSettings()
    cfg.output.original_action = "delete"
    cfg.output.set_permissions = False
    with db.session_scope() as s:
        job = Job(file_id=file_id, state="running", plan=plan.to_dict())
        s.add(job)
        s.flush()
        job_id = job.id
    outcome = encoder.EncodeOutcome(input_size=8000, output_size=7, reason="Fertig")
    replay = {"job_id": job_id, "file_id": file_id, "info": {k:v for k,v in asdict(info).items() if k != "raw"}, "plan": plan.to_dict(), "outcome": asdict(outcome), "output_mode": "replace"}
    out = tmp_path / "temp.mkv"
    out.write_bytes(b"encoded")
    real_write = durable.write_json
    def crash(path, entry):
        real_write(path, entry)
        if entry.get("phase") == phase:
            raise KeyboardInterrupt("simulated power cut")
    monkeypatch.setattr(durable, "write_json", crash)
    with pytest.raises(KeyboardInterrupt):
        output_files._commit_output(str(source), str(out), plan, cfg, info, reconciliation=replay)
    monkeypatch.setattr(durable, "write_json", real_write)
    assert output_files.recover_interrupted_commits() == 1
    assert source.read_bytes() in (b"original" * 1000, b"encoded")
    with db.session_scope() as s:
        assert s.get(Job, job_id).state == ("done" if phase == "filesystem_done" else "running")
    assert output_files.recover_interrupted_commits() == 0


def test_db_commit_replay_is_idempotent(client, tmp_path):
    file_id, path = media(tmp_path)
    with db.session_scope() as s:
        job = Job(file_id=file_id, state="running")
        s.add(job)
        s.flush()
        job_id = job.id
    outcome = encoder.EncodeOutcome(input_size=8000, output_size=4000)
    for _ in range(2):
        encoder._record_success(job_id, file_id, outcome, str(path), planner.EncodePlan(), ffmpeg.MediaInfo(path=str(path)), config.AppSettings())
    with db.session_scope() as s:
        assert s.query(HistoryEntry).count() == 1


def test_reservations_prevent_parallel_overcommit(tmp_path, monkeypatch):
    monkeypatch.setattr(scratch.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * 1024**3))
    assert not scratch.reserve(1, 6 * 1024**3, tmp_path, 1)
    assert scratch.reserve(2, 6 * 1024**3, tmp_path, 1)
    scratch.release(1)
    assert not scratch.reserve(2, 6 * 1024**3, tmp_path, 1)


def test_walk_observes_cancel_without_marking_missing(client, tmp_path):
    stop = threading.Event()
    stop.set()
    with pytest.raises(scanner.ScanCancelled):
        scanner._sync_disk_to_db(config.AppSettings(), 1, stop)


def test_quality_requires_coverage_and_reports_worst(monkeypatch):
    monkeypatch.setattr(ffmpeg, "extract_segment", AsyncMock())
    info = ffmpeg.MediaInfo(path="src", duration=120, fps=24)
    monkeypatch.setattr(quality, "measure_quality", AsyncMock(side_effect=[quality.QualityScore(97,"vmaf",97), None]))
    assert asyncio.run(output_validation._spot_check_quality("src", "out", info)) is None
    monkeypatch.setattr(quality, "measure_quality", AsyncMock(side_effect=[quality.QualityScore(97,"vmaf",97), quality.QualityScore(82,"vmaf",82)]))
    score = asyncio.run(output_validation._spot_check_quality("src", "out", info))
    assert score.successful == score.planned == 2 and score.worst_vmaf == 82


@pytest.mark.parametrize("endpoint,payload", [("/api/queue/reorder", {"order":["bad"]}), ("/api/jobs", {"file_ids":[0]}), ("/api/scan", {"depth":"bad"}), ("/api/scan", {"file_ids":[]}), ("/api/files/1/dry-run", {"force_encoder":"copy"})])
def test_invalid_payloads_return_422(client, endpoint, payload):
    assert client.post(endpoint, json=payload).status_code == 422


def test_snapshot_build_allows_invalidation_without_publishing_stale_data(client):
    with db.session_scope() as s:
        def build():
            thread = threading.Thread(target=lambda: group_cache.invalidate(s))
            thread.start()
            thread.join(timeout=1)
            assert not thread.is_alive()
            return ["old"]
        assert group_cache.snapshot(s, build) == ["old"]
        assert group_cache.snapshot(s, lambda: ["new"]) == ["new"]
