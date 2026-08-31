"""Job execution: run the encode, then decide whether to keep the result.

The rule the whole tool is built around - *a file must never come back bigger or
visibly worse* - is enforced here, not in the analyzer.  The analyzer only makes
predictions; this module measures what actually came out and throws the result
away if it does not beat the original.  Nothing overwrites an original until
every gate has passed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import AppSettings, TRANSCODE_DIR
from ..db import session_scope
from ..models import (
    FileState, HistoryEntry, Job, JobState, LearningSample, MediaFile, utcnow,
)
from . import ffmpeg, planner, predictor, quality
from .events import bus
from .hwaccel import HardwareReport
from .planner import EncodePlan

log = logging.getLogger(__name__)


@dataclass
class EncodeOutcome:
    ok: bool = False
    rejected: bool = False
    reason: str = ""
    output_size: int = 0
    input_size: int = 0
    vmaf: float | None = None          # on the VMAF scale
    quality_metric: str = ""
    elapsed: float = 0.0
    log_tail: str = ""
    fell_back_to_cpu: bool = False


class JobCancelled(Exception):
    pass


def _free_space_gb(path: str) -> float:
    try:
        usage = shutil.disk_usage(path)
        return usage.free / 1024**3
    except OSError:
        return 999.0


def _apply_ownership(path: str, settings: AppSettings) -> None:
    """Match Unraid's expected 99:100 nobody/users unless configured otherwise."""
    if not settings.output.set_permissions:
        return
    try:
        mode = int(settings.output.file_mode, 8)
        os.chmod(path, mode)
    except (OSError, ValueError) as exc:
        log.debug("chmod failed for %s: %s", path, exc)
    if hasattr(os, "chown") and os.geteuid() == 0:  # type: ignore[attr-defined]
        try:
            os.chown(path, settings.output.uid, settings.output.gid)
        except OSError as exc:
            log.debug("chown failed for %s: %s", path, exc)


def _move_to_trash(source: str, settings: AppSettings) -> str:
    """Move the original into the recycle folder, keeping its directory shape."""
    trash_root = Path(settings.output.trash_dir or "/config/trash")
    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    src = Path(source)
    # Keep enough of the path to tell two "S01E01.mkv" apart.
    parent = src.parent.name or "root"
    dest_dir = trash_root / stamp / parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{src.stem}.{counter}{src.suffix}"
        counter += 1
    shutil.move(str(src), str(dest))
    return str(dest)


def purge_trash(settings: AppSettings) -> int:
    """Delete recycled originals older than the retention window."""
    days = settings.output.trash_retention_days
    if not days:
        return 0
    root = Path(settings.output.trash_dir or "/config/trash")
    if not root.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            continue
    return removed


def _target_path(source: str, plan: EncodePlan, settings: AppSettings) -> str:
    """Where the finished file should end up."""
    src = Path(source)
    suffix = f".{plan.container}"
    cfg = settings.output
    if cfg.mode == "sidecar":
        return str(src.with_name(f"{src.stem}{cfg.sidecar_suffix}{suffix}"))
    if cfg.mode == "separate_dir" and cfg.output_dir:
        out_root = Path(cfg.output_dir)
        # Mirror the library layout underneath the output directory.
        try:
            from ..models import LibraryPath  # local import to avoid a cycle
            with session_scope() as s:
                roots = [lp.path for lp in s.query(LibraryPath).all()]
            rel = None
            for root in sorted(roots, key=len, reverse=True):
                if source.startswith(root.rstrip("/") + "/"):
                    rel = os.path.relpath(src.parent, root)
                    break
            target_dir = out_root / rel if rel and rel != "." else out_root
        except Exception:
            target_dir = out_root
        target_dir.mkdir(parents=True, exist_ok=True)
        return str(target_dir / f"{src.stem}{suffix}")
    return str(src.with_suffix(suffix))


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
    duration = max(info.duration, 1.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    last_push = 0.0

    def on_progress(p: ffmpeg.Progress) -> None:
        nonlocal last_push
        now = loop.time()
        if now - last_push < 1.0 and not p.done:
            return
        last_push = now
        progress = min(1.0, p.out_time / duration) if duration else 0.0
        elapsed = now - started
        eta = int(elapsed / progress - elapsed) if progress > 0.01 else 0
        payload = {
            "job_id": job_id, "progress": progress, "fps": p.fps, "speed": p.speed,
            "eta_seconds": eta, "current_size": p.total_size,
        }
        bus.publish("job.progress", payload)
        try:
            with session_scope() as s:
                row = s.get(Job, job_id)
                if row:
                    row.progress = progress
                    row.fps = p.fps
                    row.speed = p.speed
                    row.eta_seconds = eta
                    row.current_size = p.total_size
        except Exception:  # progress updates must never break the encode
            pass

    return await ffmpeg.run_with_progress(
        args,
        on_progress=on_progress,
        timeout=settings.encoding.max_encode_hours * 3600,
        cancel_event=cancel,
        nice=settings.queue.nice_level,
    )


async def run_job(
    job_id: int,
    settings: AppSettings,
    hw: HardwareReport | None,
    cancel: asyncio.Event,
) -> EncodeOutcome:
    """Execute one queued job end to end."""
    outcome = EncodeOutcome()
    started_wall = time.time()

    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            outcome.reason = "Job nicht gefunden"
            return outcome
        media = s.get(MediaFile, job.file_id)
        if media is None:
            outcome.reason = "Datei nicht gefunden"
            return outcome
        source = media.path
        plan_data = job.plan or media.plan
        file_id = media.id
        job.state = JobState.RUNNING.value
        job.started_at = utcnow()
        job.error = ""
        media.state = FileState.ENCODING.value

    bus.publish("job.started", {"job_id": job_id, "file_id": file_id, "path": source})

    plan = EncodePlan.from_dict(plan_data)
    if plan is None:
        return _fail(job_id, file_id, outcome, "Kein Encoding-Plan hinterlegt.")

    if not os.path.exists(source):
        return _fail(job_id, file_id, outcome, "Quelldatei existiert nicht mehr.")

    # --- disk headroom ---------------------------------------------------- #
    workdir = TRANSCODE_DIR
    workdir.mkdir(parents=True, exist_ok=True)
    free = _free_space_gb(str(workdir))
    if free < settings.queue.min_free_disk_gb:
        return _fail(
            job_id, file_id, outcome,
            f"Zu wenig freier Speicher im Arbeitsverzeichnis ({free:.0f} GB frei, "
            f"{settings.queue.min_free_disk_gb} GB gefordert).",
        )

    # --- re-probe: the file may have changed since the analysis ----------- #
    try:
        info = await ffmpeg.probe(source)
    except ffmpeg.FFmpegError as exc:
        return _fail(job_id, file_id, outcome, f"Datei nicht lesbar: {exc}")

    outcome.input_size = info.size or os.path.getsize(source)
    temp_out = workdir / f"optimizarr-{job_id}-{os.getpid()}.{plan.container}"

    try:
        code, log_tail = await _run_encode(plan, info, str(temp_out), job_id, settings, cancel)
        outcome.log_tail = log_tail

        if cancel.is_set():
            raise JobCancelled()

        # --- hardware encoders fail in creative ways; retry on the CPU ---- #
        if code != 0 and plan.is_hardware and settings.hardware.fallback_to_cpu:
            log.warning("hardware encode failed for %s, retrying on CPU", source)
            _append_log(job_id, "Hardware-Encoding fehlgeschlagen - Wiederholung mit SVT-AV1 (CPU).")
            bus.publish("job.log", {
                "job_id": job_id,
                "message": "Hardware-Encoding fehlgeschlagen - Neuversuch auf der CPU.",
            })
            plan.encoder = "libsvtav1"
            plan.hw_decode = False
            plan.pix_fmt = "yuv420p10le" if plan.pix_fmt in ("p010le", "yuv420p10le") else "yuv420p"
            outcome.fell_back_to_cpu = True
            temp_out.unlink(missing_ok=True)
            code, log_tail = await _run_encode(
                plan, info, str(temp_out), job_id, settings, cancel
            )
            outcome.log_tail = log_tail

        if cancel.is_set():
            raise JobCancelled()
        if code != 0:
            tail = "\n".join(log_tail.strip().splitlines()[-6:])
            return _fail(job_id, file_id, outcome, f"ffmpeg brach ab (Code {code}).\n{tail}")
        if not temp_out.exists():
            return _fail(job_id, file_id, outcome, "ffmpeg hat keine Ausgabedatei erzeugt.")

        outcome.output_size = temp_out.stat().st_size
        outcome.elapsed = time.time() - started_wall

        # ---------------- gate 1: is the result intact? ------------------- #
        if settings.output.verify_output:
            ok, why = await quality.verify_output(
                info, str(temp_out), settings.output.max_duration_drift_seconds
            )
            if not ok:
                return _reject(job_id, file_id, outcome, f"Ergebnis nicht plausibel: {why}")

        # ---------------- gate 2: is it actually smaller? ----------------- #
        saved = outcome.input_size - outcome.output_size
        saved_pct = (saved / outcome.input_size * 100) if outcome.input_size else 0.0
        if settings.output.require_smaller and saved <= 0:
            return _reject(
                job_id, file_id, outcome,
                f"Ergebnis waere groesser gewesen ({_fmt(outcome.output_size)} statt "
                f"{_fmt(outcome.input_size)}) - Original bleibt unveraendert.",
            )
        if saved_pct < settings.output.min_accept_saving_percent:
            return _reject(
                job_id, file_id, outcome,
                f"Nur {saved_pct:.1f}% gespart - unter der Annahmeschwelle von "
                f"{settings.output.min_accept_saving_percent:.0f}%. Original bleibt unveraendert.",
            )

        # ---------------- gate 3: did quality hold up? -------------------- #
        if settings.output.verify_vmaf:
            bus.publish("job.log", {"job_id": job_id, "message": "Qualitaet wird geprueft..."})
            score = await _spot_check_quality(source, str(temp_out), info)
            if score is not None:
                outcome.vmaf = score.vmaf_estimate
                outcome.quality_metric = score.metric
                if score.vmaf_estimate < settings.output.min_accept_vmaf:
                    return _reject(
                        job_id, file_id, outcome,
                        f"Qualitaet zu niedrig: {score.describe()} unter dem Minimum von "
                        f"{settings.output.min_accept_vmaf:.0f}.",
                    )

        # ---------------- commit ------------------------------------------ #
        final_path = await asyncio.to_thread(
            _commit_output, source, str(temp_out), plan, settings, info
        )
        outcome.ok = True
        outcome.reason = (
            f"Fertig: {_fmt(outcome.input_size)} -> {_fmt(outcome.output_size)} "
            f"({saved_pct:.0f}% gespart)"
        )
        await asyncio.to_thread(
            _record_success, job_id, file_id, outcome, final_path, plan, info, settings
        )
        return outcome

    except JobCancelled:
        outcome.reason = "Job abgebrochen"
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job:
                job.state = JobState.CANCELLED.value
                job.finished_at = utcnow()
                job.error = "Abgebrochen"
            media = s.get(MediaFile, file_id)
            if media:
                media.state = FileState.CANDIDATE.value
        bus.publish("job.finished", {"job_id": job_id, "state": "cancelled"})
        return outcome
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("job %s crashed", job_id)
        return _fail(job_id, file_id, outcome, f"Unerwarteter Fehler: {exc}")
    finally:
        try:
            if temp_out.exists():
                temp_out.unlink()
        except OSError:
            pass


async def _spot_check_quality(
    source: str, output: str, info: ffmpeg.MediaInfo
) -> quality.QualityScore | None:
    """Measure a few short slices - scoring a whole film would take hours."""
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="optimizarr-quality-", dir=str(TRANSCODE_DIR)))
    scores: list[quality.QualityScore] = []
    try:
        positions = planner.sample_positions(info.duration, 2, 0.1)
        for i, start in enumerate(positions):
            ref = workdir / f"ref{i}.mkv"
            dist = workdir / f"dist{i}.mkv"
            try:
                await ffmpeg.extract_segment(source, start, 10.0, str(ref), timeout=180)
                await ffmpeg.extract_segment(output, start, 10.0, str(dist), timeout=180)
            except ffmpeg.FFmpegError:
                continue
            score = await quality.measure_quality(str(ref), str(dist), threads=4, timeout=900)
            if score is not None:
                scores.append(score)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if not scores:
        return None
    return quality.QualityScore(
        value=sum(s.value for s in scores) / len(scores),
        metric=scores[0].metric,
        vmaf_estimate=sum(s.vmaf_estimate for s in scores) / len(scores),
    )


def _commit_output(
    source: str, temp_out: str, plan: EncodePlan, settings: AppSettings,
    info: ffmpeg.MediaInfo,
) -> str:
    """Put the finished file in its final place, safely.

    The order matters: the new file is staged next to its destination first, so
    a failed copy (full disk, permissions) never leaves the library without both
    the original and the replacement.
    """
    target = _target_path(source, plan, settings)
    target_dir = Path(target).parent
    target_dir.mkdir(parents=True, exist_ok=True)

    staging = target_dir / f".optimizarr-staging-{os.getpid()}-{Path(target).name}"
    try:
        shutil.move(temp_out, str(staging))
    except OSError:
        shutil.copy2(temp_out, str(staging))
        try:
            os.unlink(temp_out)
        except OSError:
            pass

    _apply_ownership(str(staging), settings)
    if settings.output.preserve_mtime:
        try:
            src_stat = os.stat(source)
            os.utime(staging, (src_stat.st_atime, src_stat.st_mtime))
        except OSError:
            pass

    replacing = settings.output.mode == "replace"
    if replacing:
        action = settings.output.original_action
        if action == "trash":
            _move_to_trash(source, settings)
        elif action == "delete":
            os.unlink(source)
        elif os.path.abspath(source) == os.path.abspath(target):
            # Keeping the original while writing to the same name is impossible;
            # park it beside the result instead of destroying it.
            backup = Path(source).with_suffix(f".original{Path(source).suffix}")
            shutil.move(source, str(backup))

    os.replace(str(staging), target)
    return target


def _record_success(
    job_id: int, file_id: int, outcome: EncodeOutcome, final_path: str,
    plan: EncodePlan, info: ffmpeg.MediaInfo, settings: AppSettings,
) -> None:
    """Update the database and feed the learning model."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        media = s.get(MediaFile, file_id)
        if job:
            job.state = JobState.DONE.value
            job.finished_at = utcnow()
            job.progress = 1.0
            job.output_size = outcome.output_size
            job.input_size = outcome.input_size
            job.vmaf = outcome.vmaf
            job.plan = plan.to_dict()
            job.error = ""
        if media:
            media.state = FileState.DONE.value
            media.converted_at = utcnow()
            media.measured_vmaf = outcome.vmaf

            if settings.output.mode == "replace":
                # The row now describes the new file - it took the old one's place.
                media.original_size = outcome.input_size
                media.size = outcome.output_size
                media.path = final_path
                media.container = plan.container
                media.video_codec = "av1"
                media.estimated_saving_bytes = max(0, outcome.input_size - outcome.output_size)
                media.decision_reason = outcome.reason
                try:
                    media.mtime = os.path.getmtime(final_path)
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
        duration = max(info.duration, 1.0)
        audio_bits = planner.estimate_audio_bitrate(plan, info)
        overhead = planner.estimate_overhead_bitrate(info)
        actual_total = outcome.output_size * 8 / duration
        actual_video = max(actual_total - audio_bits - overhead, 10_000.0)

        pred_input = predictor.PredictionInput(
            width=info.width, height=info.height, fps=info.fps, duration=info.duration,
            source_bitrate=info.video_bitrate, source_codec=info.video_codec,
            bit_depth=info.bit_depth, is_hdr=info.is_hdr,
            grain_level=plan.film_grain / 40.0 if plan.film_grain else 0.0,
            crf=plan.crf, preset=plan.preset, target_height=plan.target_height,
            audio_bitrate=audio_bits, overhead_bitrate=overhead,
        )
        predicted_video = plan.predicted_video_bitrate or predictor.heuristic_bitrate(pred_input)[0]
        features = predictor.build_features(pred_input, plan.encoder, has_sample=False)
        s.add(LearningSample(
            job_id=job_id,
            features=features,
            predicted_bitrate=float(predicted_video),
            actual_bitrate=float(actual_video),
            actual_vmaf=outcome.vmaf,
            encoder=plan.encoder,
            crf=plan.crf,
            source_codec=info.video_codec,
        ))
        s.add(HistoryEntry(
            level="success", category="encode", file_id=file_id,
            message=f"{Path(final_path).name}: {outcome.reason}",
            detail={
                "input_size": outcome.input_size, "output_size": outcome.output_size,
                "vmaf": outcome.vmaf, "encoder": plan.encoder, "crf": plan.crf,
                "seconds": round(outcome.elapsed),
            },
        ))

    bus.publish("job.finished", {
        "job_id": job_id, "file_id": file_id, "state": "done",
        "saved_bytes": outcome.input_size - outcome.output_size,
        "message": outcome.reason,
    })


def _append_log(job_id: int, message: str) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        job.log = (job.log or "") + f"[{stamp}] {message}\n"
        if len(job.log) > 20000:
            job.log = job.log[-20000:]


def _fail(job_id: int, file_id: int, outcome: EncodeOutcome, message: str) -> EncodeOutcome:
    outcome.ok = False
    outcome.reason = message
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
            job.error = message[:4000]
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


def _fmt(num: int | float) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024.0
    return f"{value:.1f} PiB"
