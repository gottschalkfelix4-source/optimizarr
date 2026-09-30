"""Job execution: run the encode, then decide whether to keep the result.

The analyzer predicts; this module measures the actual result and enforces the
configured gates. Forced conversion and explicit H.264 migration allow larger
files; integrity and configured quality checks still apply before replacement.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import update

from ..config import AppSettings, CONFIG_DIR, TRANSCODE_DIR
from ..db import session_scope
from ..models import (
    FileState, HistoryEntry, Job, JobState, LearningSample, MediaFile, utcnow,
)
from . import analyzer, codecs, ffmpeg, planner, predictor, quality, scratch
from .encode_types import EncodeOutcome, JobCancelled, SourceChangedError
# Public compatibility exports keep worker integrations stable after extraction.
from .output_files import (
    _commit_output, library_root_for, purge_trash, recover_interrupted_commits,
    sweep_stale_staging, original_backup_path, free_backup_path, _move_to_trash, finish_commit,
)
from .output_validation import _mid_frame, _spot_check_quality
from .events import bus
from .hwaccel import HardwareReport
from .planner import EncodePlan

log = logging.getLogger(__name__)

#: How often progress is pushed to the UI.  Fast enough that the bar keeps
#: moving on its own between updates, slow enough not to flood the socket.
PROGRESS_INTERVAL = 0.4

#: How often it is written to the database.  Only needed so a restart resumes
#: with a sane number, so a few seconds of drift costs nothing.
PERSIST_INTERVAL = 5.0

#: How long ffmpeg gets to exit after a shutdown cancelled its task.
CANCEL_GRACE = 3.0

def _persist_progress(job_id: int, snapshot: dict[str, Any]) -> None:
    """Store the latest progress.  Runs in a worker thread.

    Fire-and-forget: the write can land after the job already finished.  The
    state condition in the UPDATE itself keeps a late 97 % from overwriting the
    final 100 % (or a job that was put back into the queue).
    """
    try:
        with session_scope() as s:
            s.execute(
                update(Job)
                .where(Job.id == job_id, Job.state == JobState.RUNNING.value)
                .values(**snapshot)
            )
    except Exception:  # progress bookkeeping must never break an encode
        log.debug("could not persist progress for job %s", job_id, exc_info=True)


class CancelToken(asyncio.Event):
    """Cancel signal for one job.

    ``requeue`` tells a shutdown (container stop, update) apart from the user
    pressing cancel: the first puts the job back into the queue, the second
    ends it for good.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requeue = False


# --------------------------------------------------------------------------- #
# Disk space
# --------------------------------------------------------------------------- #

def _free_space_gb(path: str) -> float | None:
    """Free space in GiB, ``None`` when it cannot be determined.

    Unknown is not "plenty": the old fallback of 999 GB let every job start on
    an unreadable or missing work directory.
    """
    try:
        usage = shutil.disk_usage(path)
        return usage.free / 1024**3
    except OSError as exc:
        log.warning("could not determine free space of %s: %s", path, exc)
        return None


def workdir_space_problem(settings: AppSettings) -> str:
    """Why no job may start for lack of scratch space - '' when it may."""
    workdir = TRANSCODE_DIR
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Arbeitsverzeichnis {workdir} ist nicht nutzbar ({exc}) - Warteschlange wartet."
    need = settings.queue.min_free_disk_gb
    if need <= 0:
        return ""
    free = _free_space_gb(str(workdir))
    if free is None:
        return (
            f"Freier Speicher im Arbeitsverzeichnis {workdir} laesst sich nicht ermitteln - "
            "Warteschlange wartet."
        )
    if free < need:
        return (
            f"Zu wenig freier Speicher im Arbeitsverzeichnis ({free:.0f} GB frei, {need} GB "
            "gefordert) - Warteschlange wartet, bis wieder Platz ist."
        )
    return ""


def source_position(p: ffmpeg.Progress, fps: float) -> float:
    """Seconds of source encoded so far.

    ffmpeg reports `out_time=N/A` for some files for the whole run - seen with
    copied ASS subtitles and font attachments - while the frame counter keeps
    going.  Counting frames is then just as accurate for a constant-rate source.
    """
    if p.out_time > 0:
        return p.out_time
    if p.frame > 0 and fps > 0:
        return p.frame / fps
    return 0.0


# --------------------------------------------------------------------------- #
# The encode itself
# --------------------------------------------------------------------------- #

async def _run_encode(
    plan: EncodePlan,
    info: ffmpeg.MediaInfo,
    dest: str,
    job_id: int,
    settings: AppSettings,
    cancel: asyncio.Event,
) -> tuple[int, str]:
    """Run ffmpeg and stream progress into the job row."""
    args = planner.build_ffmpeg_args(plan, info, info.path, dest)
    # Unknown length: report no progress rather than 100 % after one second.
    duration = info.duration if info.duration > 0 else 0.0
    loop = asyncio.get_running_loop()
    started = loop.time()
    last_push = 0.0
    last_persist = 0.0

    def on_progress(p: ffmpeg.Progress) -> None:
        """Publish progress often, persist it rarely.

        These two have opposite needs.  The UI wants a steady stream so its bar
        moves smoothly; the database only needs enough to survive a restart.
        Writing on every update put a synchronous SQLite transaction in the
        event loop several times a second, which delayed the very messages it
        was meant to accompany.
        """
        nonlocal last_push, last_persist
        now = loop.time()
        if now - last_push < PROGRESS_INTERVAL and not p.done:
            return
        last_push = now

        elapsed = now - started
        position = source_position(p, info.fps)
        speed = p.speed or (position / elapsed if elapsed > 0 else 0.0)
        progress = min(1.0, position / duration) if duration > 0 else 0.0
        eta = int(elapsed / progress - elapsed) if progress > 0.01 else 0
        bus.publish("job.progress", {
            "job_id": job_id,
            "progress": progress,
            "fps": p.fps,
            "speed": speed,
            "eta_seconds": eta,
            "current_size": p.total_size,
            # The client extrapolates between updates; it needs to know how far
            # along the source we are and how fast that is moving.
            "out_time": position,
            "duration": duration,
        })

        if not p.done and now - last_persist < PERSIST_INTERVAL:
            return
        last_persist = now
        snapshot = {
            "progress": progress, "fps": p.fps, "speed": speed,
            "eta_seconds": eta, "current_size": p.total_size,
        }
        # Off the event loop: a blocked write must never stall the stream.
        asyncio.get_running_loop().run_in_executor(
            None, _persist_progress, job_id, snapshot
        )

    work = asyncio.ensure_future(ffmpeg.run_with_progress(
        args,
        on_progress=on_progress,
        timeout=settings.encoding.max_encode_hours * 3600,
        cancel_event=cancel,
        nice=settings.queue.nice_level,
    ))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # The task was cancelled from outside (shutdown).  The progress runner
        # would leave ffmpeg running in that case; the cancel event makes it
        # terminate the process before the task goes away.
        cancel.set()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(work, timeout=CANCEL_GRACE)
        raise


@dataclass
class _JobStart:
    source: str
    plan_data: dict[str, Any] | None
    forced: bool
    file_id: int
    known_size: int
    known_mtime: float
    library_root: str | None


def _start_job(job_id: int) -> _JobStart | str:
    """Mark the job running.  Returns why it must not run instead, if so."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return "Job nicht gefunden"
        if job.state not in (JobState.QUEUED.value, JobState.RUNNING.value):
            # Cancelled (or otherwise closed) between the claim and the start.
            return f"Job ist bereits {job.state} - nicht gestartet."
        media = s.get(MediaFile, job.file_id)
        if media is None:
            return "Datei nicht gefunden"
        start = _JobStart(
            source=media.path,
            plan_data=job.plan or media.plan,
            forced=planner.is_forced(job.plan),
            file_id=media.id,
            known_size=int(media.size or 0),
            known_mtime=float(media.mtime or 0.0),
            library_root=None,
        )
        library_id = media.library_id
        job.state = JobState.RUNNING.value
        job.started_at = utcnow()
        job.finished_at = None
        job.error = ""
        media.state = FileState.ENCODING.value
    start.library_root = library_root_for(start.source, library_id)
    return start


def _streams_match(plan: EncodePlan, info: ffmpeg.MediaInfo) -> bool:
    """Do the plan's -map indices still describe this file's streams?"""
    def planned(actions: list[dict[str, Any]]) -> set[tuple[int, str]]:
        return {(int(a.get("index", -1)), str(a.get("codec") or "")) for a in actions}

    def present(streams: list[dict[str, Any]]) -> set[tuple[int, str]]:
        return {(int(s.get("index", -1)), str(s.get("codec") or "")) for s in streams}

    return (
        planned(plan.audio) == present(info.audio_streams)
        and planned(plan.subtitles) == present(info.subtitle_streams)
    )


def dolby_vision_verdict(info: ffmpeg.MediaInfo, settings: AppSettings, forced: bool) -> str:
    """Why this Dolby Vision file must not be encoded - '' when it may.

    Judged on the probe taken at job start - the file may have been swapped
    for a DV release since the analysis, and a forced job may never have been
    analysed at all.  The rule itself is the analyzer's, so scan and job can
    never disagree: profile 5 (or DV without an HDR10 base) never, profile 7/8
    only with the HDR10 fallback setting or when forced.
    """
    reason = analyzer.dolby_vision_block(
        info.hdr_format, info.color_transfer, settings, force=forced
    )
    return f"{reason} Original bleibt unveraendert." if reason else ""


def _cpu_film_grain(plan: EncodePlan, info: ffmpeg.MediaInfo, settings: AppSettings) -> int:
    """The grain synthesis level SVT-AV1 would have been given for this file.

    Hardware plans carry 0 (the GPU encoders cannot synthesise grain), so the
    value is rebuilt the way the analyzer picks it for a CPU plan: a fixed
    setting wins, otherwise the grain measured during the analysis.
    """
    cfg = settings.encoding
    if cfg.film_grain_synthesis:
        return int(cfg.film_grain_synthesis)
    if not cfg.auto_film_grain:
        return 0
    grain = float((plan.prediction_features or {}).get("grain") or 0.0)
    return quality.grain_synthesis_level(grain, info.is_hdr) if grain else 0


async def run_job(
    job_id: int,
    settings: AppSettings,
    hw: HardwareReport | None,
    cancel: asyncio.Event,
) -> EncodeOutcome:
    """Execute one queued job end to end."""
    outcome = EncodeOutcome()
    started_wall = time.time()

    start = await asyncio.to_thread(_start_job, job_id)
    if isinstance(start, str):
        outcome.reason = start
        log.info("job %s not started: %s", job_id, start)
        return outcome
    source, file_id, forced = start.source, start.file_id, start.forced

    bus.publish("job.started", {"job_id": job_id, "file_id": file_id, "path": source})

    temp_out: Path | None = None
    try:
        if cancel.is_set():
            # Cancelled between the claim and now.
            raise JobCancelled()

        plan = EncodePlan.from_dict(start.plan_data)
        if plan is None and not forced:
            return await _fail_async(job_id, file_id, outcome, "Kein Encoding-Plan hinterlegt.")

        if not os.path.exists(source):
            return await _fail_async(job_id, file_id, outcome, "Quelldatei existiert nicht mehr.")

        # --- disk headroom ------------------------------------------------ #
        # Checked by the worker before it claims anything; this catches the
        # space filling up in between.  Out of space is the machine's problem,
        # not this file's: back into the queue instead of failing it.
        problem = await asyncio.to_thread(workdir_space_problem, settings)
        if not problem:
            problem = await asyncio.to_thread(
                scratch.reserve, job_id, scratch.required(start.known_size, plan.estimated_size if plan else 0),
                TRANSCODE_DIR, settings.queue.min_free_disk_gb,
            )
        if problem:
            await asyncio.to_thread(close_interrupted_job, job_id, True, problem)
            outcome.requeued = True
            outcome.reason = problem
            return outcome
        workdir = TRANSCODE_DIR

        # --- re-probe: the file may have changed since the analysis ------- #
        try:
            info = await ffmpeg.probe(source)
        except ffmpeg.FFmpegError as exc:
            return await _fail_async(job_id, file_id, outcome, f"Datei nicht lesbar: {exc}")
        try:
            src_stat = os.stat(source)
        except OSError as exc:
            return await _fail_async(job_id, file_id, outcome, f"Datei nicht lesbar: {exc}")
        source_signature = (src_stat.st_size, src_stat.st_mtime_ns)
        changed = bool(start.known_size) and (
            src_stat.st_size != start.known_size
            or abs(src_stat.st_mtime - start.known_mtime) > 1.0
        )

        dv_problem = dolby_vision_verdict(info, settings, forced)
        if dv_problem:
            return await _reject_async(job_id, file_id, outcome, dv_problem)

        outcome.input_size = info.size or src_stat.st_size
        problem = await asyncio.to_thread(scratch.reserve, job_id, scratch.required(outcome.input_size, plan.estimated_size if plan else 0), TRANSCODE_DIR, settings.queue.min_free_disk_gb)
        if problem:
            await asyncio.to_thread(close_interrupted_job, job_id, True, problem)
            outcome.requeued, outcome.reason = True, problem
            return outcome
        if plan is None:
            plan = await asyncio.to_thread(_plan_forced_job, job_id, info, settings, hw)
        elif changed or not _streams_match(plan, info):
            if changed and not forced and codecs.is_excluded(
                info.video_codec, settings.analysis.skip_codecs
            ):
                # Replaced behind our back by a file that is already done -
                # typically our own result after a crash mid-commit.
                return await _reject_async(job_id, file_id, outcome, (
                    f"Datei wurde seit der Analyse ersetzt und ist jetzt "
                    f"{codecs.label(info.video_codec)} - nichts zu tun."
                ))
            why = (
                "Datei wurde seit der Analyse veraendert" if changed
                else "Spuren der Datei passen nicht mehr zum gespeicherten Plan"
            )
            plan = await asyncio.to_thread(
                _replan_job, job_id, plan, info, settings, hw, why
            )

        temp_out = workdir / f"optimizarr-{job_id}-{os.getpid()}.{plan.container}"
        initial = {"encoder": plan.encoder, "hw_decode": plan.hw_decode,
                   "pix_fmt": plan.pix_fmt, "film_grain": plan.film_grain}

        code, log_tail = await _run_encode(plan, info, str(temp_out), job_id, settings, cancel)
        outcome.log_tail = log_tail

        if cancel.is_set():
            raise JobCancelled()

        # --- hardware encoders fail in creative ways; retry on the CPU ---- #
        # But only when the *video* stream is what failed.  ffmpeg tears the
        # whole pipeline down on any error, so an audio or muxer problem makes
        # every stream shout at once - retrying that on the CPU costs hours and
        # fails identically.
        if code != 0 and plan.is_hardware and not ffmpeg.failure_is_video(log_tail):
            reason = ffmpeg.first_error_line(log_tail)
            command = " ".join(
                planner.build_ffmpeg_args(plan, info, info.path, str(temp_out))
            )
            tail = "\n".join(
                f"    {ln}" for ln in log_tail.strip().splitlines()[-60:]
            )
            await _append_log_async(job_id, (
                "Encoding fehlgeschlagen - nicht am Video-Encoder, daher kein Umweg "
                "ueber die CPU.\n"
                f"  Grund: {reason}\n"
                f"  Befehl: ffmpeg {command}\n"
                f"  Vollstaendige Ausgabe:\n{tail}"
            ))
            return await _fail_async(job_id, file_id, outcome, (
                f"Encoding fehlgeschlagen: {reason} "
                "(nicht der Video-Encoder - ein Neuversuch auf der CPU wuerde genauso enden)"
            ))

        if code != 0 and plan.hw_decode:
            # Cheap middle step before giving up: decode on the CPU and keep
            # the encoder.  Some streams make the hardware decoder
            # re-initialise a few frames in ("Reconfiguring filter graph because
            # hwaccel changed"), and an all-GPU filter graph cannot be rebuilt
            # against an encoder that is already open.  The same decoder
            # hiccup breaks a GPU-decode/CPU-encode pipeline just as well.
            reason = ffmpeg.first_error_line(log_tail)
            log.warning("hardware decode path failed for %s: %s", source, reason)
            await _append_log_async(job_id, (
                f"GPU-Decoding fehlgeschlagen - Wiederholung mit CPU-Decoding, "
                f"Encoder bleibt {plan.encoder}.\n"
                f"  Grund: {reason}\n"
                f"  Vollstaendige Ausgabe:\n"
                + "\n".join(f"    {line}" for line in log_tail.strip().splitlines()[-15:])
            ))
            bus.publish("job.log", {
                "job_id": job_id,
                "message": f"GPU-Decoding fehlgeschlagen ({reason}) - Neuversuch mit CPU-Decoding.",
            })
            plan.hw_decode = False
            temp_out.unlink(missing_ok=True)
            code, log_tail = await _run_encode(
                plan, info, str(temp_out), job_id, settings, cancel
            )
            outcome.log_tail = log_tail
            if cancel.is_set():
                raise JobCancelled()

        if code != 0 and plan.is_hardware and settings.hardware.fallback_to_cpu:
            # Record *why* it failed.  A bare "fell back to CPU" is useless: the
            # GPU is then quietly unused for every future job and the reason is
            # gone, so the actual ffmpeg output and the command that produced it
            # both go into the job log.
            reason = ffmpeg.first_error_line(log_tail)
            failed_args = planner.build_ffmpeg_args(plan, info, info.path, str(temp_out))
            log.warning("hardware encode failed for %s: %s", source, reason)
            await _append_log_async(job_id, (
                f"Hardware-Encoding ({plan.encoder}) fehlgeschlagen - Wiederholung mit "
                f"SVT-AV1 (CPU).\n"
                f"  Grund: {reason}\n"
                f"  Befehl: ffmpeg {' '.join(failed_args)}\n"
                f"  Vollstaendige Ausgabe:\n"
                + "\n".join(f"    {line}" for line in log_tail.strip().splitlines()[-15:])
            ))
            bus.publish("job.log", {
                "job_id": job_id,
                "message": f"Hardware-Encoding fehlgeschlagen ({reason}) - Neuversuch auf der CPU.",
            })
            # The values of the attempt that failed - captured before the
            # plan is rewritten for the CPU below.
            detail = {
                "encoder": plan.encoder, "pix_fmt": plan.pix_fmt,
                "hw_decode": plan.hw_decode, "film_grain": plan.film_grain,
                "initial": dict(initial), "error": reason,
            }
            await asyncio.to_thread(_add_history, HistoryEntry(
                level="warning", category="encode", file_id=file_id,
                message=(
                    f"{Path(source).name}: Hardware-Encoding mit {plan.encoder} "
                    f"fehlgeschlagen - {reason}"
                ),
                detail=detail,
            ))

            plan.encoder = "libsvtav1"
            # Grain measurement survives the encoder switch; capture it before
            # discarding the GPU-specific prediction features.
            plan.film_grain = _cpu_film_grain(plan, info, settings)
            # The GPU prediction belongs to a different encoder/quality mapping.
            # Do not train or evaluate the CPU result against those features.
            plan.base_video_bitrate = 0
            plan.prediction_features = {}
            plan.predicted_video_bitrate = 0
            plan.hw_decode = False
            plan.pix_fmt = "yuv420p10le" if plan.pix_fmt in ("p010le", "yuv420p10le") else "yuv420p"
            # The hardware plan had grain synthesis switched off because the
            # GPU cannot do it; SVT-AV1 can.
            outcome.fell_back_to_cpu = True
            outcome.hw_failure_reason = reason
            temp_out.unlink(missing_ok=True)
            code, log_tail = await _run_encode(
                plan, info, str(temp_out), job_id, settings, cancel
            )
            outcome.log_tail = log_tail

        if cancel.is_set():
            raise JobCancelled()
        if code != 0:
            tail = "\n".join(log_tail.strip().splitlines()[-6:])
            return await _fail_async(
                job_id, file_id, outcome, f"ffmpeg brach ab (Code {code}).\n{tail}"
            )
        if not temp_out.exists():
            return await _fail_async(job_id, file_id, outcome, "ffmpeg hat keine Ausgabedatei erzeugt.")

        outcome.output_size = temp_out.stat().st_size
        outcome.elapsed = time.time() - started_wall

        # ---------------- gate 1: is the result intact? ------------------- #
        if settings.output.verify_output:
            ok, why = await quality.verify_output(
                info, str(temp_out), settings.output.max_duration_drift_seconds, plan=plan
            )
            if not ok:
                return await _reject_async(job_id, file_id, outcome, f"Ergebnis nicht plausibel: {why}")
        if settings.output.verify_full_decode:
            try:
                code, _, error = await ffmpeg.run_simple(
                    ["-v", "error", "-xerror", "-err_detect", "explode", "-i", str(temp_out),
                     "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
                    timeout=settings.encoding.max_encode_hours * 3600, cancel_event=cancel,
                )
            except ffmpeg.FFmpegCancelled:
                raise JobCancelled() from None
            if code:
                return await _reject_async(job_id, file_id, outcome, f"Vollstaendiges Decoding fehlgeschlagen: {error[-1000:]}")

        # ---------------- gate 2: is it actually smaller? ----------------- #
        saved = outcome.input_size - outcome.output_size
        saved_pct = (saved / outcome.input_size * 100) if outcome.input_size else 0.0
        migrate = settings.analysis.requires_h264_conversion(info.video_codec)
        if not migrate and not forced and settings.output.require_smaller and saved <= 0:
            return await _reject_async(
                job_id, file_id, outcome,
                f"Ergebnis waere groesser gewesen ({_fmt(outcome.output_size)} statt "
                f"{_fmt(outcome.input_size)}) - Original bleibt unveraendert.",
            )
        # Forced jobs explicitly accept the conversion even without savings,
        # including larger results. Integrity and quality gates still apply.
        if not migrate and not forced and saved_pct < settings.output.min_accept_saving_percent:
            return await _reject_async(
                job_id, file_id, outcome,
                f"Nur {saved_pct:.1f}% gespart - unter der Annahmeschwelle von "
                f"{settings.output.min_accept_saving_percent:.0f}%. Original bleibt unveraendert.",
            )

        # ---------------- gate 3: did quality hold up? -------------------- #
        if settings.output.verify_vmaf:
            bus.publish("job.log", {"job_id": job_id, "message": "Qualitaet wird geprueft..."})
            score = await _spot_check_quality(source, str(temp_out), info, cancel, settings.output.min_quality_samples)
            if cancel.is_set():
                raise JobCancelled()
            if score is None:
                # "Could not measure" must not read as "passed": the gate is on
                # because the user wants no original replaced unchecked.
                return await _reject_async(
                    job_id, file_id, outcome,
                    "Qualitaet konnte nicht gemessen werden (weder VMAF noch SSIM lieferten "
                    "einen Wert) - Qualitaetspruefung ist aktiv, Original bleibt unveraendert.",
                )
            outcome.vmaf = score.vmaf_estimate
            outcome.quality_metric = score.metric
            outcome.quality_value = score.value
            outcome.quality_details = {"successful": score.successful, "planned": score.planned, "worst_vmaf": score.worst_vmaf}
            gate_score = score.worst_vmaf if score.worst_vmaf is not None else score.vmaf_estimate
            await _append_log_async(job_id, f"Qualitaetspruefung: {score.successful}/{score.planned} Ausschnitte, schlechtester VMAF-Wert/SSIM-Schaetzung {gate_score:.1f}.")
            if gate_score < settings.output.min_accept_vmaf:
                return await _reject_async(
                    job_id, file_id, outcome,
                    f"Qualitaet zu niedrig: schlechtester Ausschnitt {gate_score:.1f} ({score.describe()}) unter dem Minimum von "
                    f"{settings.output.min_accept_vmaf:.0f}.",
                )

        # A cancel that arrived during the checks still wins over the commit.
        if cancel.is_set():
            raise JobCancelled()

        # ---------------- commit ------------------------------------------ #
        notes: list[str] = []
        size_change = f"{saved_pct:.0f}% gespart" if saved >= 0 else f"{-saved_pct:.0f}% groesser"
        outcome.reason = (
            f"Fertig: {_fmt(outcome.input_size)} -> {_fmt(outcome.output_size)} "
            f"({size_change})"
        )
        outcome.elapsed = time.time() - started_wall

        async def finalize() -> None:
            replay = {
                "job_id": job_id, "file_id": file_id, "outcome": asdict(outcome),
                "plan": plan.to_dict(), "info": {k: v for k, v in asdict(info).items() if k != "raw"},
                # No credentials belong in an on-disk commit manifest.
                "output_mode": settings.output.mode,
            }
            final_path = await asyncio.to_thread(
                _commit_output, source, str(temp_out), plan, settings, info,
                source_signature, start.library_root, notes, replay,
            )
            final_info = None
            if settings.output.mode == "replace":
                try:
                    final_info = await ffmpeg.probe(final_path)
                except ffmpeg.FFmpegError as exc:
                    log.warning("could not re-read %s after the encode: %s", final_path, exc)
            await asyncio.to_thread(
                _record_success, job_id, file_id, outcome, final_path, plan, info, settings, final_info,
            )
            await asyncio.to_thread(finish_commit, job_id)
            outcome.ok = True
            for note in notes:
                await _append_log_async(job_id, note)

        # A thread cannot be cancelled halfway through a filesystem mutation.
        # Finish both durable steps before shutdown is allowed to close this job.
        finalizer = asyncio.create_task(finalize(), name=f"optimizarr-commit-{job_id}")
        try:
            await asyncio.shield(finalizer)
        except asyncio.CancelledError:
            await finalizer
        except SourceChangedError as exc:
            return await _fail_async(job_id, file_id, outcome, str(exc))
        return outcome

    except JobCancelled:
        requeue = bool(getattr(cancel, "requeue", False))
        outcome.reason = "Job zurueck in die Warteschlange" if requeue else "Job abgebrochen"
        outcome.requeued = requeue
        await asyncio.to_thread(close_interrupted_job, job_id, requeue)
        return outcome
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("job %s crashed", job_id)
        return await _fail_async(job_id, file_id, outcome, f"Unerwarteter Fehler: {exc}")
    finally:
        scratch.release(job_id)
        if temp_out is not None:
            try:
                if temp_out.exists():
                    temp_out.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Bookkeeping
# --------------------------------------------------------------------------- #

def _apply_probe(media: MediaFile, info: ffmpeg.MediaInfo) -> None:
    """Copy everything the probe says about the file onto its row."""
    media.container = info.container or media.container
    media.video_codec = info.video_codec
    media.profile = info.profile
    media.width = info.width
    media.height = info.height
    media.fps = info.fps
    media.duration = info.duration
    media.video_bitrate = info.video_bitrate
    media.bit_depth = info.bit_depth
    media.pix_fmt = info.pix_fmt
    media.is_hdr = info.is_hdr
    media.hdr_format = info.hdr_format
    media.color_primaries = info.color_primaries
    media.color_transfer = info.color_transfer
    media.color_space = info.color_space
    media.interlaced = info.interlaced
    media.audio_streams = info.audio_streams
    media.subtitle_streams = info.subtitle_streams


def _learning_sample(
    job_id: int, outcome: EncodeOutcome, plan: EncodePlan, info: ffmpeg.MediaInfo,
) -> LearningSample | None:
    """The (features, base, actual) triple the model is fitted on.

    It has to match how the model is *applied*: the analyzer looks the
    correction up with ``plan.prediction_features`` and multiplies it onto
    ``plan.base_video_bitrate``.  Training on the already corrected bitrate, or
    on features rebuilt differently, taught the model its own output.
    """
    duration = max(info.duration, 1.0)
    audio_bits = planner.estimate_audio_bitrate(plan, info)
    overhead = planner.estimate_overhead_bitrate(info)
    actual_total = outcome.output_size * 8 / duration
    actual_video = max(actual_total - audio_bits - overhead, 10_000.0)

    features = {k: float(v) for k, v in (plan.prediction_features or {}).items()
                if isinstance(v, (int, float))}
    base = float(plan.base_video_bitrate or 0)
    if features and base > 0:
        if outcome.fell_back_to_cpu and features.get("is_hw_encoder"):
            if features.get("has_sample"):
                # The base is a trial encode on the GPU; a CPU result says
                # nothing about how far off that measurement was.
                log.info("job %s: CPU fallback - learning sample discarded", job_id)
                return None
            # The heuristic base does not depend on the encoder; only the
            # encoder feature has to say what actually ran.
            features["is_hw_encoder"] = 0.0
    else:
        # Plan from before base/features were stored.  Its corrected
        # prediction cannot be used; recompute the heuristic pair from the
        # same input instead - base and features then belong together.
        inp = predictor.PredictionInput(
            width=info.width, height=info.height, fps=info.fps, duration=info.duration,
            source_bitrate=info.video_bitrate, source_codec=info.video_codec,
            bit_depth=info.bit_depth, is_hdr=info.is_hdr,
            is_animation=quality.looks_like_animation(info.path),
            crf=plan.crf, preset=plan.preset, target_height=plan.target_height,
            audio_bitrate=audio_bits, overhead_bitrate=overhead,
        )
        base, features = predictor.heuristic_training_pair(inp, plan.encoder)

    return LearningSample(
        job_id=job_id,
        features=features,
        predicted_bitrate=float(base),
        applied_bitrate=float(plan.predicted_video_bitrate) if plan.predicted_video_bitrate > 0 else None,
        actual_bitrate=float(actual_video),
        actual_vmaf=outcome.vmaf,
        quality_metric=outcome.quality_metric or None,
        quality_value=outcome.quality_value,
        encoder=plan.encoder,
        crf=plan.crf,
        source_codec=info.video_codec,
    )


def _record_success(
    job_id: int, file_id: int, outcome: EncodeOutcome, final_path: str,
    plan: EncodePlan, info: ffmpeg.MediaInfo, settings: AppSettings,
    final_info: ffmpeg.MediaInfo | None = None,
) -> None:
    """Update the database and feed the learning model."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        media = s.get(MediaFile, file_id)
        if job is not None and job.state == JobState.DONE.value:
            return  # journal replay after DB commit must not duplicate history/learning
        if job is None or media is None:
            raise RuntimeError("Job oder Datei fuer die Ergebnisverbuchung fehlt.")
        if job:
            job.state = JobState.DONE.value
            job.finished_at = utcnow()
            job.progress = 1.0
            job.output_size = outcome.output_size
            job.input_size = outcome.input_size
            job.vmaf = outcome.vmaf
            job.quality_metric = outcome.quality_metric or None
            job.quality_value = outcome.quality_value
            job.quality_details = outcome.quality_details
            job.plan = {**plan.to_dict(), **planner.job_markers(job.plan)}
            job.error = ""
        if media:
            media.state = FileState.DONE.value
            media.converted_at = utcnow()
            media.measured_vmaf = outcome.vmaf
            media.quality_metric = outcome.quality_metric or None
            media.quality_value = outcome.quality_value
            media.quality_details = outcome.quality_details

            if settings.output.mode == "replace":
                # The row now describes the new file - it took the old one's place.
                media.original_size = outcome.input_size
                media.path = final_path
                if final_info is not None:
                    _apply_probe(media, final_info)
                else:
                    media.container = plan.container
                    media.video_codec = "av1"
                    media.bit_depth = 10 if "10" in plan.pix_fmt else 8
                media.size = outcome.output_size
                # The estimate is replaced by what actually happened - bytes
                # and percent together, or the library shows a saving with 0 %.
                saved = max(0, outcome.input_size - outcome.output_size)
                media.estimated_size = outcome.output_size
                media.estimated_saving_bytes = saved
                media.estimated_saving_pct = (
                    saved / outcome.input_size * 100.0 if outcome.input_size else 0.0
                )
                media.decision_reason = outcome.reason
                media.error = ""
                try:
                    st = os.stat(final_path)
                    media.size = st.st_size
                    media.mtime = st.st_mtime
                except OSError:
                    pass
            else:
                # sidecar / separate_dir: the source is still on disk untouched, so
                # the row must keep describing it.  Claiming a saving here would be
                # wrong - both files exist, nothing was freed yet.
                media.estimated_saving_bytes = 0
                media.estimated_saving_pct = 0.0
                media.decision_reason = (
                    f"{outcome.reason} Die AV1-Fassung liegt unter {final_path}; "
                    "das Original wurde nicht angetastet."
                )

        # --- learning sample --------------------------------------------- #
        sample = _learning_sample(job_id, outcome, plan, info)
        if sample is not None:
            s.add(sample)
        s.add(HistoryEntry(
            level="success", category="encode", file_id=file_id,
            message=f"{Path(final_path).name}: {outcome.reason}",
            detail={
                "input_size": outcome.input_size, "output_size": outcome.output_size,
                "vmaf": outcome.vmaf, "encoder": plan.encoder, "crf": plan.crf,
                "seconds": round(outcome.elapsed),
                "cpu_fallback": outcome.fell_back_to_cpu,
            },
        ))

    bus.publish("job.finished", {
        "job_id": job_id, "file_id": file_id, "state": "done",
        "saved_bytes": outcome.input_size - outcome.output_size,
        "message": outcome.reason,
    })


def reconcile_commit(entry: dict[str, Any]) -> None:
    """Replay committed output bookkeeping before orphan jobs are requeued."""
    data = entry["reconciliation"]
    settings = AppSettings()
    settings.output.mode = data["output_mode"]
    outcome = EncodeOutcome(**data["outcome"])
    outcome.ok = True
    plan = EncodePlan.from_dict(data["plan"])
    if plan is None:
        raise ValueError("Wiederherstellungsjournal enthaelt keinen gueltigen Plan.")
    info = ffmpeg.MediaInfo(**data["info"])
    _record_success(data["job_id"], data["file_id"], outcome, entry["target"], plan, info, settings)


def _plan_forced_job(
    job_id: int, info: ffmpeg.MediaInfo, settings: AppSettings, hw: HardwareReport | None,
) -> EncodePlan:
    """Plan a forced job whose file was excluded before the analysis built one
    (codec, size, bitrate floor) - from the probe just taken, the way the dry
    run does.  Stored on the job so the queue can show it.

    Dolby Vision is not re-checked here: run_job applies
    ``analyzer.dolby_vision_block`` to the same probe before any plan is built.
    """
    plan = planner.build_plan(info, settings, hw)
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job:
            job.plan = {**plan.to_dict(), **planner.job_markers(job.plan)}
    _append_log(job_id, f"Erzwungen ohne Analyse - Plan beim Start erstellt: {plan.describe()}")
    return plan


def _replan_job(
    job_id: int, old: EncodePlan, info: ffmpeg.MediaInfo, settings: AppSettings,
    hw: HardwareReport | None, why: str,
) -> EncodePlan:
    """Rebuild the plan against the file as it is now.

    The stored plan maps streams by index; run against a file that changed, it
    maps the wrong tracks or fails on ones that no longer exist.  The quality
    decisions of the analysis (encoder, CRF, grain) are kept; stream handling,
    pixel format and scaling come from the fresh probe.  The size prediction
    no longer belongs to this file, so it is not used for learning.
    """
    plan = planner.build_plan(
        info, settings, hw, crf=old.crf, film_grain=old.film_grain, encoder=old.encoder,
    )
    plan.base_video_bitrate = 0
    plan.prediction_features = {}
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job:
            job.plan = {**plan.to_dict(), **planner.job_markers(job.plan)}
    _append_log(job_id, f"{why} - Plan beim Start neu erstellt: {plan.describe()}")
    return plan


def close_interrupted_job(job_id: int, requeue: bool, note: str = "") -> str:
    """End a running job that did not finish.  Returns the new state ('' if untouched).

    ``requeue`` (shutdown, no scratch space): back into the queue as if it
    never started.  Otherwise the user cancelled it: closed for good, and the
    file goes back to the state it had before it was queued.
    """
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None or job.state != JobState.RUNNING.value:
            return ""
        media = s.get(MediaFile, job.file_id)
        if requeue:
            job.state = JobState.QUEUED.value
            job.progress = 0.0
            job.fps = 0.0
            job.speed = 0.0
            job.eta_seconds = 0
            job.current_size = 0
            job.started_at = None
            job.finished_at = None
            job.error = ""
            message = note or "Optimizarr wurde beendet - der Job startet beim naechsten Mal neu."
            job.log = (job.log or "") + f"[{stamp}] {message}\n"
            if media and media.state == FileState.ENCODING.value:
                media.state = FileState.QUEUED.value
            new_state = JobState.QUEUED.value
        else:
            job.state = JobState.CANCELLED.value
            job.finished_at = utcnow()
            job.error = "Abgebrochen"
            job.log = (job.log or "") + f"[{stamp}] Vom Benutzer abgebrochen.\n"
            if media and media.state in (FileState.ENCODING.value, FileState.QUEUED.value):
                # A forced job goes back to wherever the file was, not to "candidate".
                media.state = (job.plan or {}).get(planner.RESTORE_STATE) or (
                    FileState.CANDIDATE.value if media.plan else FileState.PROBED.value
                )
            new_state = JobState.CANCELLED.value
    if requeue:
        bus.publish("queue.changed", {})
    else:
        bus.publish("job.finished", {"job_id": job_id, "state": "cancelled"})
    return new_state


def _add_history(entry: HistoryEntry) -> None:
    with session_scope() as s:
        s.add(entry)


def _append_log(job_id: int, message: str) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        job.log = (job.log or "") + f"[{stamp}] {message}\n"
        if len(job.log) > 20000:
            job.log = job.log[-20000:]


async def _append_log_async(job_id: int, message: str) -> None:
    await asyncio.to_thread(_append_log, job_id, message)


def _fail(job_id: int, file_id: int, outcome: EncodeOutcome, message: str) -> EncodeOutcome:
    outcome.ok = False
    outcome.reason = message
    from .output_files import pending_commits
    if job_id in pending_commits()["jobs"]:
        # Filesystem outcome is durable; do not turn it into another encode.
        outcome.reason = f"Ergebnisverbuchung wartet auf Wiederherstellung: {message}"
        log.error("job %s has a pending commit: %s", job_id, message)
        return outcome
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job:
            job.state = JobState.FAILED.value
            job.finished_at = utcnow()
            job.error = message[:4000]
            if outcome.log_tail:
                job.log = (job.log or "") + "\n" + outcome.log_tail[-6000:]
        media = s.get(MediaFile, file_id)
        if media:
            media.state = FileState.FAILED.value
            media.error = message[:2000]
        s.add(HistoryEntry(level="error", category="encode", file_id=file_id,
                           message=message[:800]))
    bus.publish("job.finished", {"job_id": job_id, "state": "failed", "message": message})
    log.error("job %s failed: %s", job_id, message)
    return outcome


async def _fail_async(
    job_id: int, file_id: int, outcome: EncodeOutcome, message: str
) -> EncodeOutcome:
    return await asyncio.to_thread(_fail, job_id, file_id, outcome, message)


def _reject(job_id: int, file_id: int, outcome: EncodeOutcome, message: str) -> EncodeOutcome:
    """The encode ran but the result was not worth keeping."""
    outcome.ok = False
    outcome.rejected = True
    outcome.reason = message
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job:
            job.state = JobState.REJECTED.value
            job.finished_at = utcnow()
            job.output_size = outcome.output_size
            job.input_size = outcome.input_size
            job.vmaf = outcome.vmaf
            job.quality_metric = outcome.quality_metric or None
            job.quality_value = outcome.quality_value
            job.error = message[:4000]
            job.quality_details = outcome.quality_details
        media = s.get(MediaFile, file_id)
        if media:
            # Remember the verdict so a later scan does not retry the same thing.
            media.state = FileState.SKIPPED.value
            media.decision_reason = message
            media.estimated_saving_bytes = 0
            media.estimated_saving_pct = 0.0
            media.analyzed_at = utcnow()
        s.add(HistoryEntry(level="warning", category="encode", file_id=file_id,
                           message=message[:800]))
    bus.publish("job.finished", {"job_id": job_id, "state": "rejected", "message": message})
    log.info("job %s rejected: %s", job_id, message)
    return outcome


async def _reject_async(
    job_id: int, file_id: int, outcome: EncodeOutcome, message: str
) -> EncodeOutcome:
    return await asyncio.to_thread(_reject, job_id, file_id, outcome, message)


def _fmt(num: int | float) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024.0
    return f"{value:.1f} PiB"
