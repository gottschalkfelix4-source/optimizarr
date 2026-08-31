"""Background workers: the encode queue, the scan schedule, housekeeping."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from sqlalchemy import func, select

from ..config import AppSettings, load_settings
from ..db import session_scope
from ..models import Job, JobState, LearningSample, MediaFile, FileState
from . import encoder, hwaccel, predictor, scanner
from .events import bus

log = logging.getLogger(__name__)

POLL_SECONDS = 5


def within_schedule(settings: AppSettings, now: dt.datetime | None = None) -> tuple[bool, str]:
    """Is the encoder allowed to run right now?"""
    cfg = settings.queue
    if not cfg.schedule_enabled:
        return True, ""
    now = now or dt.datetime.now()
    if now.weekday() not in (cfg.schedule_days or list(range(7))):
        return False, f"Heute ({now.strftime('%A')}) ist kein Encoding-Tag."
    try:
        sh, sm = (int(x) for x in cfg.schedule_start.split(":"))
        eh, em = (int(x) for x in cfg.schedule_end.split(":"))
    except (ValueError, AttributeError):
        return True, ""
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start <= end:
        allowed = start <= now <= end
    else:  # window crosses midnight
        allowed = now >= start or now <= end
    if allowed:
        return True, ""
    return False, f"Ausserhalb des Zeitfensters ({cfg.schedule_start}-{cfg.schedule_end})."


class QueueWorker:
    """Pulls jobs off the queue and runs them, honouring the schedule."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running: dict[int, asyncio.Task] = {}
        self._cancels: dict[int, asyncio.Event] = {}
        self._stop = asyncio.Event()
        self.blocked_reason = ""

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="optimizarr-queue")
            log.info("queue worker started")

    async def stop(self) -> None:
        self._stop.set()
        for event in self._cancels.values():
            event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()
        self._cancels.clear()

    # -- state -------------------------------------------------------------- #

    @property
    def active_job_ids(self) -> list[int]:
        return list(self._running.keys())

    def cancel_job(self, job_id: int) -> bool:
        event = self._cancels.get(job_id)
        if event is not None:
            event.set()
            return True
        return False

    def status(self) -> dict[str, Any]:
        settings = load_settings()
        allowed, reason = within_schedule(settings)
        return {
            "running_jobs": self.active_job_ids,
            "paused": settings.queue.paused,
            "schedule_ok": allowed,
            "blocked_reason": self.blocked_reason or ("" if allowed else reason),
            "max_concurrent": settings.queue.max_concurrent_jobs,
        }

    # -- main loop ---------------------------------------------------------- #

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - keep the worker alive
                log.exception("queue worker tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        settings = load_settings()

        if settings.queue.paused:
            self.blocked_reason = "Warteschlange ist pausiert."
            return
        allowed, reason = within_schedule(settings)
        if not allowed:
            self.blocked_reason = reason
            return
        if scanner.state.running and settings.queue.max_concurrent_jobs <= 1:
            # Sharing one CPU between an analysis pass and an encode makes both
            # crawl; let the scan finish first.
            self.blocked_reason = "Bibliotheks-Scan laeuft - Encoding wartet."
            return

        slots = settings.queue.max_concurrent_jobs - len(self._running)
        if slots <= 0:
            self.blocked_reason = ""
            return

        job_ids = await asyncio.to_thread(self._claim_jobs, slots)
        if not job_ids:
            self.blocked_reason = ""
            return

        hw = hwaccel.cached()
        if hw is None:
            hw = await hwaccel.detect(
                settings.hardware.render_device, settings.hardware.qsv_low_power
            )
        for job_id in job_ids:
            cancel = asyncio.Event()
            self._cancels[job_id] = cancel
            task = asyncio.create_task(
                self._run_one(job_id, settings, hw, cancel), name=f"optimizarr-job-{job_id}"
            )
            self._running[job_id] = task

    def _claim_jobs(self, limit: int) -> list[int]:
        """Reserve the next jobs so a second tick cannot pick them up twice."""
        claimed: list[int] = []
        with session_scope() as s:
            rows = s.execute(
                select(Job)
                .where(Job.state == JobState.QUEUED.value)
                .order_by(Job.priority.asc(), Job.created_at.asc())
                .limit(limit)
            ).scalars().all()
            for job in rows:
                job.state = JobState.RUNNING.value
                claimed.append(job.id)
        return claimed

    async def _run_one(
        self, job_id: int, settings: AppSettings, hw: Any, cancel: asyncio.Event
    ) -> None:
        try:
            await encoder.run_job(job_id, settings, hw, cancel)
        except asyncio.CancelledError:
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job and job.state == JobState.RUNNING.value:
                    job.state = JobState.CANCELLED.value
                    job.error = "Abgebrochen"
            raise
        except Exception:
            log.exception("job %s raised", job_id)
        finally:
            self._running.pop(job_id, None)
            self._cancels.pop(job_id, None)
            bus.publish("queue.changed", {})
            try:
                await asyncio.to_thread(refit_predictor)
            except Exception:
                log.debug("predictor refit failed", exc_info=True)


def refit_predictor() -> dict[str, Any]:
    """Re-train the size model on everything learned so far."""
    settings = load_settings()
    with session_scope() as s:
        rows = s.execute(
            select(LearningSample).order_by(LearningSample.created_at.desc()).limit(2000)
        ).scalars().all()
        samples = [
            {
                "features": r.features or {},
                "predicted_bitrate": r.predicted_bitrate,
                "actual_bitrate": r.actual_bitrate,
            }
            for r in rows
        ]
    model = predictor.refit(samples, settings.analysis.trust_learning_after_samples)
    stats = model.stats()
    bus.publish("model.updated", stats)
    return stats


class Scheduler:
    """Periodic library scan plus daily housekeeping."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.next_scan: dt.datetime | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="optimizarr-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def _compute_next(self, settings: AppSettings) -> dt.datetime | None:
        hours = settings.library.scan_interval_hours
        if not hours:
            return None
        with session_scope() as s:
            from ..models import ScanRun
            last = s.execute(
                select(func.max(ScanRun.started_at)).where(ScanRun.state == "done")
            ).scalar()
        base = last or dt.datetime.now(dt.timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=dt.timezone.utc)
        return base + dt.timedelta(hours=hours)

    async def _loop(self) -> None:
        last_purge = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        while not self._stop.is_set():
            try:
                settings = load_settings()
                now = dt.datetime.now(dt.timezone.utc)
                self.next_scan = self._compute_next(settings)

                if (
                    self.next_scan is not None
                    and now >= self.next_scan
                    and not scanner.state.running
                ):
                    log.info("starting scheduled library scan")
                    asyncio.create_task(scanner.run_scan(trigger="schedule"))

                if (now - last_purge).total_seconds() > 86400:
                    last_purge = now
                    removed = await asyncio.to_thread(encoder.purge_trash, settings)
                    if removed:
                        log.info("purged %d files from trash", removed)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass


queue_worker = QueueWorker()
scheduler = Scheduler()


def enqueue_files(file_ids: list[int], priority: int | None = None) -> tuple[int, list[str]]:
    """Add files to the queue.  Returns (count, skipped_reasons)."""
    added = 0
    skipped: list[str] = []
    with session_scope() as s:
        for file_id in file_ids:
            media = s.get(MediaFile, file_id)
            if media is None:
                skipped.append(f"#{file_id}: nicht gefunden")
                continue
            if media.plan is None:
                skipped.append(f"{media.path}: noch nicht analysiert")
                continue
            existing = s.execute(
                select(Job).where(
                    Job.file_id == file_id,
                    Job.state.in_([JobState.QUEUED.value, JobState.RUNNING.value]),
                )
            ).scalars().first()
            if existing:
                skipped.append(f"{media.path}: steht bereits in der Warteschlange")
                continue
            prio = priority if priority is not None else 100 - min(
                99, int(media.estimated_saving_pct)
            )
            s.add(Job(
                file_id=file_id, plan=media.plan, input_size=media.size,
                predicted_size=media.estimated_size, priority=prio,
            ))
            media.state = FileState.QUEUED.value
            added += 1
    if added:
        bus.publish("queue.changed", {"added": added})
    return added, skipped
