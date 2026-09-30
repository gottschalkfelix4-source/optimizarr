"""Background workers: the encode queue, the scan schedule, housekeeping."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from ..config import AppSettings, load_settings
from ..db import session_scope
from ..models import HistoryEntry, Job, JobState, LearningSample, MediaFile, FileState
from . import background, encoder, hwaccel, planner, predictor, scanner, scratch
from .events import bus
from . import maintenance

log = logging.getLogger(__name__)

POLL_SECONDS = 5

#: German day names for messages - strftime('%A') follows the C locale.
WEEKDAYS_DE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")

#: How long a shutdown waits for running encodes to wind down.  Docker sends
#: SIGKILL ten seconds after SIGTERM, so this has to stay well below that.
STOP_GRACE_SECONDS = 7.0

#: A scheduled scan that failed is retried after this long at the earliest
#: (or after the scan interval, if that is shorter) - not on every tick.
SCAN_RETRY_HOURS = 1.0


def within_schedule(settings: AppSettings, now: dt.datetime | None = None) -> tuple[bool, str]:
    """Is the encoder allowed to run right now?"""
    cfg = settings.queue
    if not cfg.schedule_enabled:
        return True, ""
    now = now or dt.datetime.now()
    if now.weekday() not in cfg.schedule_days:
        return False, f"Heute ({WEEKDAYS_DE[now.weekday()]}) ist kein Encoding-Tag."
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
        self._cancels: dict[int, encoder.CancelToken] = {}
        self._stop = asyncio.Event()
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self.blocked_reason = ""
        self.blocked_kind = ""
        #: "Jetzt starten": ignore the schedule until the queue has run dry.
        #: Kept in memory only - after a restart the window applies again.
        self.schedule_override = False

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._event_loop = asyncio.get_running_loop()
            self._stopping = False
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="optimizarr-queue")
            log.info("queue worker started")

    async def stop(self, grace: float = STOP_GRACE_SECONDS) -> None:
        """Wind down for a container stop or update.

        Running encodes are interrupted and go back into the queue - they did
        nothing wrong, and "cancelled" would drop them for good.  ffmpeg is
        stopped through the cancel token, so the job itself removes its
        temporary file and resets its row.
        """
        self._stopping = True
        self._stop.set()
        for token in list(self._cancels.values()):
            token.requeue = True
            token.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        running = list(self._running.values())
        if running:
            _, pending = await asyncio.wait(running, timeout=grace)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        # Claimed in the last moment but never started: back into the queue.
        for job_id in list(self._cancels):
            if job_id not in self._running:
                try:
                    encoder.close_interrupted_job(job_id, True)
                except Exception:
                    log.exception("could not requeue job %s", job_id)
                finally:
                    scratch.release(job_id)
        self._running.clear()
        self._cancels.clear()

    # -- state -------------------------------------------------------------- #

    @property
    def active_job_ids(self) -> list[int]:
        return list(self._running.keys())

    def cancel_job(self, job_id: int) -> bool:
        """Ask a running job to stop.  Safe to call from any thread.

        The API calls this from its thread pool, and asyncio.Event is not
        thread-safe: set() from there need not wake the waiting encode.
        """
        token = self._cancels.get(job_id)
        if token is None:
            return False
        loop = self._event_loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or running is loop or loop.is_closed():
            token.set()
        else:
            loop.call_soon_threadsafe(token.set)
        return True

    def start_now(self) -> None:
        """Run the queue now, whatever the schedule says, until it is empty."""
        if not self.schedule_override:
            log.info("schedule overridden: starting the queue now")
        self.schedule_override = True

    def end_override(self) -> None:
        """Back to the schedule - by hand, or once the queue has run dry."""
        if self.schedule_override:
            log.info("schedule override ended")
        self.schedule_override = False

    def _set_blocked(self, kind: str, reason: str) -> None:
        if kind == "disk" and self.blocked_kind != "disk":
            log.warning("queue on hold: %s", reason)
        elif self.blocked_kind == "disk" and kind != "disk":
            log.info("queue resumes: scratch space available again")
        self.blocked_kind = kind
        self.blocked_reason = reason

    def status(self) -> dict[str, Any]:
        settings = load_settings()
        allowed, reason = within_schedule(settings)
        # Pause and schedule are read fresh; disk and scan are what the last
        # tick found.
        if settings.queue.paused:
            kind, text = "paused", "Warteschlange ist pausiert."
        elif not allowed and not self.schedule_override:
            kind, text = "schedule", reason
        elif self.blocked_kind in ("disk", "scan"):
            kind, text = self.blocked_kind, self.blocked_reason
        else:
            kind, text = "", ""
        return {
            "running_jobs": self.active_job_ids,
            "paused": settings.queue.paused,
            "schedule_ok": allowed,
            "schedule_override": self.schedule_override,
            "blocked_reason": text,
            "blocked_kind": kind,
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
        if not self._running:
            await background.run_in_thread(encoder.recover_interrupted_commits)
        settings = load_settings()

        if settings.queue.paused:
            self._set_blocked("paused", "Warteschlange ist pausiert.")
            return
        allowed, reason = within_schedule(settings)
        if not allowed and not self.schedule_override:
            self._set_blocked("schedule", reason)
            return
        if scanner.state.running and settings.queue.max_concurrent_jobs <= 1:
            # Sharing one CPU between an analysis pass and an encode makes both
            # crawl; let the scan finish first.
            self._set_blocked("scan", "Bibliotheks-Scan laeuft - Encoding wartet.")
            return

        slots = settings.queue.max_concurrent_jobs - len(self._running)
        if slots <= 0:
            self._set_blocked("", "")
            return

        # Out of scratch space is a property of the machine, not of the next
        # file: hold the queue instead of failing it job by job (seen live:
        # 27 jobs failed within a minute on a full /transcode).
        problem = await asyncio.to_thread(encoder.workdir_space_problem, settings)
        if problem:
            self._set_blocked("disk", problem)
            return

        if not await asyncio.to_thread(_has_queued_jobs):
            if not self._running and self.schedule_override:
                # Everything that was waiting is done: new jobs follow the
                # schedule again instead of starting in the middle of the day.
                self.end_override()
                bus.publish("queue.changed", {})
            self._set_blocked("", "")
            return

        # Hardware first: the claim is the last await before the jobs are
        # handed to their tasks.
        hw = hwaccel.cached()
        if hw is None:
            hw = await hwaccel.detect(
                settings.hardware.render_device, settings.hardware.qsv_low_power
            )
        if self._stop.is_set():
            return

        self._set_blocked("", "")
        job_ids = await background.run_in_thread(self._claim_jobs, slots)
        for job_id in job_ids:
            cancel = self._cancels[job_id]
            if self._stopping:
                cancel.requeue = True
                cancel.set()
            task = asyncio.create_task(
                self._run_one(job_id, settings, hw, cancel), name=f"optimizarr-job-{job_id}"
            )
            self._running[job_id] = task

    @maintenance.claim_guard
    def _claim_jobs(self, limit: int) -> list[int]:
        """Reserve the next jobs so a second tick cannot pick them up twice.

        The cancel token is registered *before* the claim commits: the API sees
        a job as running from that moment on, and a cancel pressed right then
        used to find no token and close the job underneath the starting encode.
        """
        claimed: list[int] = []
        try:
            with session_scope() as s:
                rows = s.execute(
                    select(Job)
                    .where(Job.state == JobState.QUEUED.value)
                    .order_by(Job.priority.asc(), Job.created_at.asc())
                    .limit(limit)
                ).scalars().all()
                for job in rows:
                    cfg = load_settings()
                    media = s.get(MediaFile, job.file_id)
                    need = scratch.required(max(job.input_size or 0, media.size if media else 0), job.predicted_size or 0)
                    problem = scratch.reserve(job.id, need, encoder.TRANSCODE_DIR, cfg.queue.min_free_disk_gb)
                    if problem:
                        self._set_blocked("disk", problem)
                        break
                    self._cancels[job.id] = encoder.CancelToken()
                    job.state = JobState.RUNNING.value
                    claimed.append(job.id)
        except BaseException:
            for job_id in claimed:
                scratch.release(job_id)
                self._cancels.pop(job_id, None)
            raise
        return claimed

    async def _run_one(
        self, job_id: int, settings: AppSettings, hw: Any, cancel: encoder.CancelToken
    ) -> None:
        try:
            await encoder.run_job(job_id, settings, hw, cancel)
        except asyncio.CancelledError:
            # Torn down from outside - a shutdown that ran out of patience.
            # Blocking on purpose: the task is going away and the row must not
            # stay "running".
            try:
                encoder.close_interrupted_job(
                    job_id, self._stopping or bool(getattr(cancel, "requeue", False))
                )
            except Exception:
                log.exception("could not close job %s after cancellation", job_id)
            raise
        except Exception as exc:
            # run_job only guards the encode itself; anything before it (an
            # unwritable /transcode, a locked database) used to leave the job
            # "running" and the file "encoding" until the next container restart.
            log.exception("job %s raised", job_id)
            try:
                await asyncio.to_thread(_fail_crashed_job, job_id, exc)
            except Exception:
                log.exception("could not mark job %s as failed", job_id)
        finally:
            scratch.release(job_id)
            self._running.pop(job_id, None)
            self._cancels.pop(job_id, None)
            bus.publish("queue.changed", {})
            if not self._stopping:
                try:
                    await asyncio.to_thread(refit_predictor)
                except Exception:
                    log.debug("predictor refit failed", exc_info=True)


def _has_queued_jobs() -> bool:
    with session_scope() as s:
        return s.execute(
            select(Job.id).where(Job.state == JobState.QUEUED.value).limit(1)
        ).first() is not None


def _fail_crashed_job(job_id: int, exc: BaseException) -> None:
    """Close out a job whose run_job raised instead of returning."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None or job.state != JobState.RUNNING.value:
            return
        file_id = job.file_id
    encoder._fail(job_id, file_id, encoder.EncodeOutcome(), f"Unerwarteter Fehler: {exc}")


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


#: Written once the learning samples of the old GPU quality handling are gone.
HW_SAMPLES_RESET_MARKER = "hw-learning-samples-reset.done"
#: Encoders whose quality value used to be wrong: ``av1_vaapi`` ignored the
#: CRF entirely (every encode ran at the driver default, q_idx 25) and
#: ``av1_qsv`` read the CRF unchanged as ICQ.  Samples from those encodes
#: describe a CRF that was never applied and would pull the model away from
#: what ``planner.hw_quality`` does now.
LEGACY_HW_SAMPLE_ENCODERS = ("av1_vaapi", "av1_qsv")


def reset_legacy_hw_samples(config_dir: Path) -> int:
    """Once: drop the learning samples recorded with the old GPU quality values.

    Returns how many were deleted.  The caller refits the predictor afterwards.
    Nothing happens (and no marker is written) when the database cannot be
    read, so the next start tries again.
    """
    marker = config_dir / HW_SAMPLES_RESET_MARKER
    try:
        if marker.exists():
            return 0
    except OSError:
        return 0
    with session_scope() as s:
        removed = s.execute(
            delete(LearningSample).where(LearningSample.encoder.in_(LEGACY_HW_SAMPLE_ENCODERS))
        ).rowcount or 0
        if removed:
            s.add(HistoryEntry(
                level="info", category="system",
                message=(
                    f"Lernmodell bereinigt: {removed} Messwert(e) aus GPU-Encodes "
                    "(av1_vaapi/av1_qsv) mit der alten Qualitaetseinstellung entfernt - "
                    "sie passen nicht zur neuen CRF-Umrechnung."
                ),
                detail={"removed": removed, "encoders": list(LEGACY_HW_SAMPLE_ENCODERS)},
            ))
    if removed:
        log.info("removed %d learning sample(s) of the old GPU quality handling", removed)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(dt.datetime.now(dt.timezone.utc).isoformat() + "\n")
    except OSError as exc:
        log.warning("could not write %s: %s", marker, exc)
    return removed


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


class Scheduler:
    """Periodic library scan plus daily housekeeping."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Strong references: the loop only keeps weak ones, and a scan task
        # nobody holds can be garbage-collected mid-run.
        self._tasks: set[asyncio.Task] = set()
        self._last_launch: dt.datetime | None = None
        self.next_scan: dt.datetime | None = None
        self._first_scan_base: dt.datetime | None = None

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

    def _spawn(self, coro: Any, name: str) -> asyncio.Task:
        from . import background
        task = background.spawn(coro, name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _compute_next(self, settings: AppSettings) -> dt.datetime | None:
        """When the next scheduled scan is due.

        Counted from the last *successful* scan - but an attempt since then
        that did not succeed pushes it back by the retry delay.  Without that, a
        scan failing on an unreachable share was restarted every minute.
        """
        hours = settings.library.scan_interval_hours
        if not hours:
            return None
        with session_scope() as s:
            from ..models import ScanRun
            last_done = s.execute(
                select(func.max(ScanRun.started_at)).where(ScanRun.state == "done")
            ).scalar()
            last_try = s.execute(select(func.max(ScanRun.started_at))).scalar()
        last_done, last_try = _aware(last_done), _aware(last_try)
        if self._first_scan_base is None:
            self._first_scan_base = dt.datetime.now(dt.timezone.utc)
        base = last_done or last_try or self._first_scan_base
        due = base + dt.timedelta(hours=hours)
        retry = dt.timedelta(hours=min(float(hours), SCAN_RETRY_HOURS))
        for attempt in (last_try, self._last_launch):
            if attempt is not None and (last_done is None or attempt > last_done):
                due = max(due, attempt + retry)
        return due

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
                    self._last_launch = now
                    self._spawn(scanner.run_scan(trigger="schedule"), "optimizarr-scheduled-scan")

                if (now - last_purge).total_seconds() > 86400:
                    last_purge = now
                    removed = await background.run_in_thread(encoder.purge_trash, settings)
                    if removed:
                        log.info("purged %d files from trash", removed)
                    from . import upkeep
                    await background.run_in_thread(upkeep.prune, settings)
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


@maintenance.enqueue_guard
def enqueue_files(
    file_ids: list[int], priority: int | None = None, force: bool = False,
) -> tuple[int, list[str]]:
    """Add files to the queue.  Returns (count, skipped_reasons).

    ``force`` queues a file whatever the analysis said about it - excluded
    codec, too small, not worth it, ignored.  A file excluded before a plan was
    built gets one when its job starts (see ``encoder.run_job``).
    """
    added = 0
    skipped: list[str] = []
    from .output_files import pending_commits
    pending = pending_commits()["files"]
    with session_scope() as s:
        for file_id in file_ids:
            if file_id in pending:
                skipped.append(f"#{file_id}: Ergebnisverbuchung wartet auf Wiederherstellung")
                continue
            media = s.get(MediaFile, file_id)
            if media is None:
                skipped.append(f"#{file_id}: nicht gefunden")
                continue
            if media.state == FileState.MISSING.value:
                skipped.append(f"{media.path}: fehlt auf der Platte")
                continue
            if media.state == FileState.DONE.value:
                skipped.append(f"{media.path}: bereits konvertiert")
                continue
            if not force and (media.ignored or media.state != FileState.CANDIDATE.value):
                skipped.append(f"{media.path}: kein freigegebener Kandidat; Erzwingen erforderlich")
                continue
            if media.plan is None and not force:
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
            plan = media.plan
            if force:
                plan = {
                    **(media.plan or {}),
                    planner.FORCED: True,
                    planner.RESTORE_STATE: media.state,
                }
            prio = priority if priority is not None else 100 - min(
                99, int(media.estimated_saving_pct)
            )
            s.add(Job(
                file_id=file_id, plan=plan, input_size=media.size,
                predicted_size=media.estimated_size, priority=prio,
            ))
            media.state = FileState.QUEUED.value
            added += 1
    if added:
        bus.publish("queue.changed", {"added": added})
    return added, skipped
