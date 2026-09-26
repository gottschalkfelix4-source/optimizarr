"""Queue worker: blocking, cancelling, shutting down, recovering."""
import asyncio
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(Path(tempfile.gettempdir()) / "optimizarr-pytest/config"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db, main
from app.config import AppSettings
from app.core import encoder, planner, scanner, worker
from app.core.ffmpeg import MediaInfo
from app.models import Base, FileState, HistoryEntry, Job, JobState, MediaFile, ScanRun


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(encoder, "TRANSCODE_DIR", tmp_path / "transcode")
    monkeypatch.setattr(encoder, "COMMIT_JOURNAL_DIR", tmp_path / "config" / "pending-commits", raising=False)
    monkeypatch.setattr(worker.hwaccel, "cached", lambda: SimpleNamespace())
    monkeypatch.setattr(worker, "refit_predictor", lambda: {})
    monkeypatch.setattr(scanner.state, "running", False, raising=False)
    yield
    engine.dispose()


def queued_job(tmp_path, state=JobState.QUEUED.value):
    source = tmp_path / "lib" / "Film.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x" * 100)
    plan = planner.EncodePlan().to_dict()
    with db.session_scope() as s:
        media = MediaFile(path=str(source), state=FileState.QUEUED.value, plan=plan)
        s.add(media)
        s.flush()
        job = Job(file_id=media.id, plan=plan, state=state)
        s.add(job)
        s.flush()
        return source, job.id, media.id


def rows(job_id, file_id):
    with db.session_scope() as s:
        return s.get(Job, job_id), s.get(MediaFile, file_id)


def encode_until_cancelled(started):
    async def fake(plan, info, dest, job_id, settings, cancel):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"partial")
        started.set()
        await cancel.wait()
        return 255, "Exiting normally, received signal 15."
    return fake


def patch_run(monkeypatch, tmp_path, fake):
    monkeypatch.setattr(encoder, "workdir_space_problem", lambda settings: "")
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(
        side_effect=lambda path: MediaInfo(path=str(path), size=100, duration=60, width=1920,
                                           height=1080, fps=24, video_codec="hevc")))
    monkeypatch.setattr(encoder, "_run_encode", fake)


# --- 8. schedule and disk ---------------------------------------------------- #

def test_schedule_message_names_the_day_in_german():
    cfg = AppSettings()
    cfg.queue.schedule_enabled = True
    cfg.queue.schedule_days = [0, 1, 2, 3, 4]
    allowed, reason = worker.within_schedule(cfg, dt.datetime(2026, 8, 30, 12, 0))  # a Sunday
    assert not allowed and "Sonntag" in reason


def test_full_scratch_disk_holds_the_queue_instead_of_failing_it(tmp_path, monkeypatch):
    _, job_id, file_id = queued_job(tmp_path)
    monkeypatch.setattr(encoder, "_free_space_gb", lambda path: 2.0)
    run_job = AsyncMock()
    monkeypatch.setattr(encoder, "run_job", run_job)
    w = worker.QueueWorker()
    asyncio.run(w._tick())
    assert not run_job.called
    job, media = rows(job_id, file_id)
    assert job.state == JobState.QUEUED.value and media.state == FileState.QUEUED.value
    status = w.status()
    assert status["blocked_kind"] == "disk"
    assert "Speicher" in status["blocked_reason"]


def test_status_reports_the_kind_of_block(monkeypatch):
    cfg = AppSettings()
    cfg.queue.paused = True
    monkeypatch.setattr(worker, "load_settings", lambda: cfg)
    assert worker.QueueWorker().status()["blocked_kind"] == "paused"
    cfg.queue.paused = False
    cfg.queue.schedule_enabled = True
    cfg.queue.schedule_days = []
    cfg.queue.schedule_start, cfg.queue.schedule_end = "00:00", "00:00"
    assert worker.QueueWorker().status()["blocked_kind"] == "schedule"


# --- 6. cancel races ---------------------------------------------------------- #

def test_claimed_job_can_be_cancelled_before_its_task_exists(tmp_path):
    _, job_id, _ = queued_job(tmp_path)
    w = worker.QueueWorker()
    assert w._claim_jobs(1) == [job_id]
    assert w.cancel_job(job_id)          # the API sees "running" from here on
    assert w._cancels[job_id].is_set()


def test_cancel_from_a_thread_wakes_the_loop_safely():
    async def scenario():
        loop = asyncio.get_running_loop()
        w = worker.QueueWorker()
        w._event_loop = loop
        token = encoder.CancelToken()
        w._cancels[7] = token
        calls = []
        real = loop.call_soon_threadsafe
        loop.call_soon_threadsafe = lambda *a: (calls.append(a), real(*a))[1]
        try:
            assert await asyncio.to_thread(w.cancel_job, 7)
            await asyncio.wait_for(token.wait(), 2)
        finally:
            loop.call_soon_threadsafe = real
        return calls

    assert asyncio.run(scenario())


# --- 5. user cancel vs. shutdown ------------------------------------------------ #

def _start_and(tmp_path, monkeypatch, then):
    source, job_id, file_id = queued_job(tmp_path)
    started = asyncio.Event()

    async def scenario():
        nonlocal started
        started = asyncio.Event()
        patch_run(monkeypatch, tmp_path, encode_until_cancelled(started))
        w = worker.QueueWorker()
        w._event_loop = asyncio.get_running_loop()
        await w._tick()
        await asyncio.wait_for(started.wait(), 5)
        await then(w, job_id)

    asyncio.run(scenario())
    return rows(job_id, file_id)


def test_shutdown_requeues_running_jobs(tmp_path, monkeypatch):
    async def shutdown(w, job_id):
        await w.stop(grace=5)

    job, media = _start_and(tmp_path, monkeypatch, shutdown)
    assert job.state == JobState.QUEUED.value
    assert job.progress == 0 and job.started_at is None and job.finished_at is None
    assert "beendet" in job.log
    assert media.state == FileState.QUEUED.value
    assert not any((tmp_path / "transcode").glob("optimizarr-*"))


def test_user_cancel_closes_the_job_and_restores_the_file(tmp_path, monkeypatch):
    async def cancel(w, job_id):
        assert await asyncio.to_thread(w.cancel_job, job_id)
        await asyncio.wait_for(asyncio.gather(*w._running.values()), 5)

    job, media = _start_and(tmp_path, monkeypatch, cancel)
    assert job.state == JobState.CANCELLED.value and job.finished_at is not None
    assert media.state == FileState.CANDIDATE.value


def test_task_cancellation_still_closes_the_job_properly(tmp_path, monkeypatch):
    source, job_id, file_id = queued_job(tmp_path)

    async def scenario():
        started = asyncio.Event()

        async def hang(plan, info, dest, job_id, settings, cancel):
            started.set()
            await asyncio.sleep(3600)

        patch_run(monkeypatch, tmp_path, hang)
        w = worker.QueueWorker()
        await w._tick()
        await asyncio.wait_for(started.wait(), 5)
        task = w._running[job_id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    job, media = rows(job_id, file_id)
    assert job.state == JobState.CANCELLED.value and job.finished_at is not None
    assert media.state == FileState.CANDIDATE.value


# --- 13. scheduler ------------------------------------------------------------ #

def test_failed_scan_is_not_retried_every_minute():
    now = dt.datetime.now(dt.timezone.utc)
    with db.session_scope() as s:
        s.add(ScanRun(state="done", started_at=now - dt.timedelta(hours=30)))
        s.add(ScanRun(state="failed", started_at=now - dt.timedelta(minutes=10)))
    cfg = AppSettings()
    cfg.library.scan_interval_hours = 24
    due = worker.Scheduler()._compute_next(cfg)
    assert due >= now + dt.timedelta(minutes=45)


def test_scheduler_keeps_its_scan_task_referenced(monkeypatch):
    release = asyncio.Event

    async def scenario():
        gate = release()

        async def fake_scan(trigger="manual"):
            await gate.wait()

        monkeypatch.setattr(worker.scanner, "run_scan", fake_scan)
        cfg = AppSettings()
        cfg.library.scan_interval_hours = 1
        monkeypatch.setattr(worker, "load_settings", lambda: cfg)
        monkeypatch.setattr(worker.encoder, "purge_trash", lambda settings: 0)
        with db.session_scope() as s:
            s.add(ScanRun(state="done", started_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)))
        sched = worker.Scheduler()
        sched.start()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if sched._tasks:
                break
        held = len(sched._tasks)
        gate.set()
        await sched.stop()
        return held

    assert asyncio.run(scenario()) == 1


# --- 5./14. start-up recovery (main.py) --------------------------------------- #

def test_recover_orphans_counts_jobs_and_files():
    with db.session_scope() as s:
        a = MediaFile(path="/lib/a.mkv", state=FileState.ENCODING.value, plan={"crf": 30})
        b = MediaFile(path="/lib/b.mkv", state=FileState.ANALYZING.value, video_codec="h264")
        s.add_all([a, b])
        s.flush()
        s.add(Job(file_id=a.id, state=JobState.RUNNING.value, progress=0.4))
        ids = a.id, b.id
    main._recover_orphans()
    with db.session_scope() as s:
        assert s.get(MediaFile, ids[0]).state == FileState.QUEUED.value
        assert s.get(MediaFile, ids[1]).state == FileState.PROBED.value  # never a planless candidate
        job = s.query(Job).one()
        assert job.state == JobState.QUEUED.value and job.progress == 0
        message = s.query(HistoryEntry).one().message
    assert "1 Job(s)" in message and "2 Datei(en)" in message


def test_clean_start_sweeps_staging_next_to_queued_files(tmp_path, monkeypatch):
    source, job_id, _ = queued_job(tmp_path)
    stale = source.parent / ".optimizarr-staging-9-deadbeef-Film.mkv"
    stale.write_bytes(b"old encode")
    monkeypatch.setattr(main, "TRANSCODE_DIR", tmp_path / "transcode")
    main._clean_transcode_dir()
    assert not stale.exists()
    assert source.exists()
