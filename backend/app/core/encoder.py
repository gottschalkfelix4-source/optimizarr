"""Job execution: run the encode, then decide whether to keep the result.

The analyzer predicts; this module measures the actual result and enforces the
configured gates. Explicit H.264 migration allows larger files, while integrity
and configured quality checks still apply before an original is replaced.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import errno
import json
import logging
import math
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import update

from ..config import AppSettings, CONFIG_DIR, TRANSCODE_DIR
from ..db import session_scope
from ..models import (
    FileState, HistoryEntry, Job, JobState, LearningSample, LibraryPath, MediaFile, utcnow,
)
from . import analyzer, codecs, ffmpeg, planner, predictor, quality
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

#: Recycle folder created inside each library root when no trash_dir is set.
TRASH_DIRNAME = ".optimizarr-trash"
STAGING_PREFIX = ".optimizarr-staging-"
BACKUP_PREFIX = ".optimizarr-backup-"

#: One JSON file per commit in flight, so a crash half-way through replacing an
#: original can be rolled back on the next start (see recover_interrupted_commits).
COMMIT_JOURNAL_DIR = CONFIG_DIR / "pending-commits"
#: Every recycle folder ever used, so purge_trash finds them all again.
TRASH_ROOTS_FILE = CONFIG_DIR / "trash-roots.json"

#: Room left over when moving a file onto another filesystem.
_SPACE_MARGIN = 256 * 1024 * 1024

_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STAGING_RE = re.compile(r"^\.optimizarr-staging-\d+-[0-9a-f]{8}-(.+)$")


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


@dataclass
class EncodeOutcome:
    ok: bool = False
    rejected: bool = False
    requeued: bool = False             # put back into the queue, nothing ran
    reason: str = ""
    output_size: int = 0
    input_size: int = 0
    vmaf: float | None = None          # on the VMAF scale
    quality_metric: str = ""
    elapsed: float = 0.0
    log_tail: str = ""
    fell_back_to_cpu: bool = False
    hw_failure_reason: str = ""        # why the GPU path was abandoned


class JobCancelled(Exception):
    pass


class SourceChangedError(RuntimeError):
    """The original was modified while it was being encoded."""


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


def _existing_ancestor(path: Path) -> Path:
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _ensure_room(dest_dir: Path, size: int, source: str, what: str) -> None:
    """Refuse a move onto another filesystem that would not fit.

    A move within one filesystem is a rename and needs no room at all; across
    filesystems it is a full copy, and running out half-way is the worst time.
    """
    try:
        anchor = _existing_ancestor(dest_dir)
        if os.stat(anchor).st_dev == os.stat(source).st_dev:
            return
        free = shutil.disk_usage(anchor).free
    except OSError as exc:
        raise OSError(f"Freier Platz fuer {what} ({dest_dir}) nicht ermittelbar: {exc}") from exc
    if free < size + _SPACE_MARGIN:
        raise OSError(
            f"Zu wenig Platz fuer {what} in {dest_dir}: {_fmt(free)} frei, "
            f"{_fmt(size)} benoetigt - nichts ersetzt."
        )


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #

def _apply_ownership(path: str, settings: AppSettings, directory: bool = False) -> None:
    """Match Unraid's expected 99:100 nobody/users unless configured otherwise."""
    if not settings.output.set_permissions:
        return
    try:
        mode = int(settings.output.file_mode, 8)
        if directory:
            # 0664 -> 0775: a folder needs x wherever it has r.
            mode |= (mode & 0o444) >> 2
        os.chmod(path, mode)
    except (OSError, ValueError) as exc:
        log.debug("chmod failed for %s: %s", path, exc)
    if hasattr(os, "chown") and os.geteuid() == 0:  # type: ignore[attr-defined]
        try:
            os.chown(path, settings.output.uid, settings.output.gid)
        except OSError as exc:
            log.debug("chown failed for %s: %s", path, exc)


def _make_dirs(path: Path, settings: AppSettings) -> None:
    """mkdir -p, giving every folder we create the configured owner and mode."""
    missing: list[Path] = []
    current = Path(path)
    while not current.exists() and current.parent != current:
        missing.append(current)
        current = current.parent
    for folder in reversed(missing):
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        _apply_ownership(str(folder), settings, directory=True)


# --------------------------------------------------------------------------- #
# Kept originals and the recycle folder
# --------------------------------------------------------------------------- #

def original_backup_path(source: str) -> Path:
    """Where a kept original is parked: ``Film.mkv`` -> ``Film.original.mkv``."""
    src = Path(source)
    return src.with_suffix(f".original{src.suffix}")


def free_backup_path(source: str) -> Path:
    """The first ``.original`` name not taken yet.

    ``Film.original.mkv``, then ``Film.1.original.mkv``, ``Film.2.original.mkv``
    ...  An earlier kept original is a different, older file and must never be
    overwritten.  The number goes before the marker because the scanner skips
    names whose stem ends in ``.original``.
    """
    first = original_backup_path(source)
    if not os.path.lexists(first):
        return first
    src = Path(source)
    counter = 1
    while True:
        candidate = src.with_name(f"{src.stem}.{counter}.original{src.suffix}")
        if not os.path.lexists(candidate):
            return candidate
        counter += 1


def library_root_for(path: str, library_id: int | None = None) -> str | None:
    """The library folder a file belongs to, or None."""
    try:
        with session_scope() as s:
            if library_id:
                lp = s.get(LibraryPath, library_id)
                if lp is not None and (
                    path == lp.path or path.startswith(lp.path.rstrip("/") + "/")
                ):
                    return lp.path
            roots = [lp.path for lp in s.query(LibraryPath).all()]
    except Exception:
        log.debug("could not look up the library of %s", path, exc_info=True)
        return None
    for root in sorted(roots, key=len, reverse=True):
        if path.startswith(root.rstrip("/") + "/"):
            return root
    return None


def trash_root(source: str, settings: AppSettings, library_root: str | None = None) -> Path:
    """The recycle folder for this file.

    Empty ``trash_dir``: ``<library>/.optimizarr-trash`` - on the same
    filesystem, so recycling is a rename and never a full copy.  Without a
    known library the file's own folder stands in.
    """
    if settings.output.trash_dir:
        return Path(settings.output.trash_dir)
    root = library_root or library_root_for(source)
    base = Path(root) if root else Path(source).parent
    return base / TRASH_DIRNAME


def _load_trash_roots() -> list[str]:
    try:
        data = json.loads(TRASH_ROOTS_FILE.read_text(encoding="utf-8"))
        return [str(p) for p in data if isinstance(p, str)]
    except (OSError, ValueError):
        return []


def _register_trash_root(root: Path) -> None:
    roots = _load_trash_roots()
    if str(root) in roots:
        return
    roots.append(str(root))
    try:
        TRASH_ROOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TRASH_ROOTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(roots), encoding="utf-8")
        os.replace(tmp, TRASH_ROOTS_FILE)
    except OSError as exc:
        log.warning("could not remember trash folder %s: %s", root, exc)


def _prepare_trash(
    source: str, settings: AppSettings, library_root: str | None = None,
) -> Path:
    """Pick (and create) the destination for a recycled original.

    Runs before the original is touched: a full or unwritable recycle folder
    has to stop the replacement, not strand the original half-way.
    """
    lib = library_root or library_root_for(source)
    root = trash_root(source, settings, lib)
    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    src = Path(source)
    rel: str | None = None
    if lib and source.startswith(lib.rstrip("/") + "/"):
        rel = os.path.relpath(src.parent, lib)
    if not rel or rel == "." or rel.startswith(".."):
        # Keep enough of the path to tell two "S01E01.mkv" apart.
        rel = src.parent.name or "root"
    dest_dir = root / stamp / rel
    _ensure_room(dest_dir, os.path.getsize(source), source, "den Papierkorb")
    _make_dirs(dest_dir, settings)
    _register_trash_root(root)
    dest = dest_dir / src.name
    counter = 1
    while os.path.lexists(dest):
        dest = dest_dir / f"{src.stem}.{counter}{src.suffix}"
        counter += 1
    return dest


def _move_file(src: str, dest: Path) -> None:
    """Rename, or copy-and-delete across filesystems - never a half copy left behind."""
    try:
        os.rename(src, dest)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    try:
        shutil.copy2(src, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise
    os.unlink(src)


def _move_to_trash(
    source: str, settings: AppSettings, library_root: str | None = None,
    from_path: str | None = None, dest: Path | None = None,
) -> str:
    """Move the original into the recycle folder, keeping its directory shape.

    ``from_path`` is where the original actually sits right now (a safety
    link while its name already holds the new file); it is filed under the
    name ``source`` had.
    """
    if dest is None:
        dest = _prepare_trash(source, settings, library_root)
    _move_file(from_path or source, dest)
    # The retention clock starts now.  A move keeps the original's mtime, so a
    # file from 2019 looked years overdue and was purged within a day.
    try:
        os.utime(dest)
    except OSError as exc:
        log.warning("could not stamp %s for the trash retention: %s", dest, exc)
    return str(dest)


def _trash_roots(settings: AppSettings) -> list[Path]:
    """Every recycle folder that may hold something to purge."""
    candidates: list[Path] = []
    if settings.output.trash_dir:
        candidates.append(Path(settings.output.trash_dir))
    try:
        with session_scope() as s:
            candidates += [Path(lp.path) / TRASH_DIRNAME for lp in s.query(LibraryPath).all()]
    except Exception:
        log.debug("could not list library folders for the trash purge", exc_info=True)
    candidates += [Path(p) for p in _load_trash_roots()]
    candidates.append(CONFIG_DIR / "trash")  # the old default location
    seen: set[str] = set()
    roots: list[Path] = []
    for root in candidates:
        key = os.path.abspath(root)
        if key in seen or key == "/" or not root.is_dir():
            continue
        seen.add(key)
        roots.append(root)
    return roots


def purge_trash(settings: AppSettings) -> int:
    """Delete recycled originals older than the retention window.

    Covers every recycle folder - one per library plus a configured one.  Only
    the dated folders the trash itself creates are looked into, so a
    mistyped ``trash_dir`` pointing at a media share cannot empty it.
    """
    days = settings.output.trash_retention_days
    if not days:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for root in _trash_roots(settings):
        try:
            stamps = [d for d in root.iterdir() if d.is_dir() and _STAMP_RE.match(d.name)]
        except OSError:
            continue
        for stamp_dir in stamps:
            for path in sorted(stamp_dir.rglob("*"), reverse=True):
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                    elif path.is_dir() and not any(path.iterdir()):
                        path.rmdir()
                except OSError:
                    continue
            with contextlib.suppress(OSError):
                if not any(stamp_dir.iterdir()):
                    stamp_dir.rmdir()
    return removed


# --------------------------------------------------------------------------- #
# Target paths
# --------------------------------------------------------------------------- #

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
            plan.hw_decode = False
            plan.pix_fmt = "yuv420p10le" if plan.pix_fmt in ("p010le", "yuv420p10le") else "yuv420p"
            # The hardware plan had grain synthesis switched off because the
            # GPU cannot do it; SVT-AV1 can.
            plan.film_grain = _cpu_film_grain(plan, info, settings)
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

        # ---------------- gate 2: is it actually smaller? ----------------- #
        saved = outcome.input_size - outcome.output_size
        saved_pct = (saved / outcome.input_size * 100) if outcome.input_size else 0.0
        migrate = settings.analysis.requires_h264_conversion(info.video_codec)
        if not migrate and settings.output.require_smaller and saved <= 0:
            return await _reject_async(
                job_id, file_id, outcome,
                f"Ergebnis waere groesser gewesen ({_fmt(outcome.output_size)} statt "
                f"{_fmt(outcome.input_size)}) - Original bleibt unveraendert.",
            )
        # A forced job was queued knowing the analysis expected little or
        # nothing; the saving threshold would reject exactly what was asked
        # for.  "Never bigger than the original" above still applies.
        if not migrate and not forced and saved_pct < settings.output.min_accept_saving_percent:
            return await _reject_async(
                job_id, file_id, outcome,
                f"Nur {saved_pct:.1f}% gespart - unter der Annahmeschwelle von "
                f"{settings.output.min_accept_saving_percent:.0f}%. Original bleibt unveraendert.",
            )

        # ---------------- gate 3: did quality hold up? -------------------- #
        if settings.output.verify_vmaf:
            bus.publish("job.log", {"job_id": job_id, "message": "Qualitaet wird geprueft..."})
            score = await _spot_check_quality(source, str(temp_out), info, cancel)
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
            if score.vmaf_estimate < settings.output.min_accept_vmaf:
                return await _reject_async(
                    job_id, file_id, outcome,
                    f"Qualitaet zu niedrig: {score.describe()} unter dem Minimum von "
                    f"{settings.output.min_accept_vmaf:.0f}.",
                )

        # A cancel that arrived during the checks still wins over the commit.
        if cancel.is_set():
            raise JobCancelled()

        # ---------------- commit ------------------------------------------ #
        notes: list[str] = []
        try:
            final_path = await asyncio.to_thread(
                _commit_output, source, str(temp_out), plan, settings, info,
                source_signature, start.library_root, notes,
            )
        except SourceChangedError as exc:
            return await _fail_async(job_id, file_id, outcome, str(exc))
        for note in notes:
            await _append_log_async(job_id, note)

        outcome.ok = True
        size_change = f"{saved_pct:.0f}% gespart" if saved >= 0 else f"{-saved_pct:.0f}% groesser"
        outcome.reason = (
            f"Fertig: {_fmt(outcome.input_size)} -> {_fmt(outcome.output_size)} "
            f"({size_change})"
        )
        # The row must describe the file that is now on disk, not the one the
        # scan saw months ago.
        final_info: ffmpeg.MediaInfo | None = None
        if settings.output.mode == "replace":
            try:
                final_info = await ffmpeg.probe(final_path)
            except ffmpeg.FFmpegError as exc:
                log.warning("could not re-read %s after the encode: %s", final_path, exc)
        await asyncio.to_thread(
            _record_success, job_id, file_id, outcome, final_path, plan, info, settings,
            final_info,
        )
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
        if temp_out is not None:
            try:
                if temp_out.exists():
                    temp_out.unlink()
            except OSError:
                pass


def _mid_frame(start: float, fps: float) -> float:
    """Move a cut position to halfway between two frames.

    The quality check pairs the two slices frame by frame, so both must begin
    on the same picture.  Source and encode may disagree about a timestamp by
    a millisecond (Matroska rounding); a cut exactly on a frame could then
    start one slice a frame later than the other.  Half a frame away from
    every timestamp, both cuts pick the same first frame.
    """
    if fps <= 0 or start <= 0:
        return start
    return (math.floor(start * fps) + 0.5) / fps


async def _spot_check_quality(
    source: str, output: str, info: ffmpeg.MediaInfo,
    cancel: asyncio.Event | None = None,
) -> quality.QualityScore | None:
    """Measure a few short slices - scoring a whole film would take hours.

    ``cancel`` stops the cuts and comparisons that are running; the job then
    ends as cancelled (:class:`JobCancelled`), not as a failed measurement.
    """
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="optimizarr-quality-", dir=str(TRANSCODE_DIR)))
    scores: list[quality.QualityScore] = []
    try:
        positions = planner.sample_positions(info.duration, 2, 0.1)
        for i, start in enumerate(positions):
            if cancel is not None and cancel.is_set():
                raise JobCancelled()
            start = _mid_frame(start, info.fps)
            ref = workdir / f"ref{i}.mkv"
            dist = workdir / f"dist{i}.mkv"
            try:
                await ffmpeg.extract_segment(
                    source, start, 10.0, str(ref), timeout=180, exact=True, cancel_event=cancel,
                )
                await ffmpeg.extract_segment(
                    output, start, 10.0, str(dist), timeout=180, exact=True, cancel_event=cancel,
                )
            except ffmpeg.FFmpegCancelled:
                raise JobCancelled() from None
            except ffmpeg.FFmpegError as exc:
                log.warning("quality slice at %.0fs failed: %s", start, exc)
                continue
            # The source size from the probe: a downscaled encode is judged on
            # the canvas the viewer actually sees.
            try:
                score = await quality.measure_quality(
                    str(ref), str(dist), threads=4, timeout=900,
                    width=info.width, height=info.height, cancel_event=cancel,
                )
            except ffmpeg.FFmpegCancelled:
                raise JobCancelled() from None
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


# --------------------------------------------------------------------------- #
# Commit: put the result in place without ever losing both files
# --------------------------------------------------------------------------- #

def _fsync_file(path: str | Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as exc:
        # Some FUSE mounts cannot fsync at all; that is not a reason to stop.
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
    finally:
        os.close(fd)


def _fsync_dir(path: str | Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _check_source_unchanged(source: str, expected: tuple[int, int] | None) -> None:
    """Refuse to replace an original that changed since the encode began.

    A download client or *arr upgrading the release mid-encode would otherwise
    have its new file thrown away for an encode of the old one.
    """
    if expected is None:
        return
    try:
        st = os.stat(source)
    except FileNotFoundError:
        raise SourceChangedError(
            "Quelldatei ist waehrend des Encodings verschwunden - Ergebnis verworfen, "
            "nichts ersetzt."
        ) from None
    size, mtime_ns = expected
    if st.st_size != size or st.st_mtime_ns != mtime_ns:
        raise SourceChangedError(
            f"Quelldatei wurde waehrend des Encodings veraendert ({_fmt(size)} -> "
            f"{_fmt(st.st_size)}) - Ergebnis verworfen, Original bleibt unberuehrt."
        )


def _journal_write(entry: dict[str, Any]) -> Path | None:
    try:
        COMMIT_JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        path = COMMIT_JOURNAL_DIR / f"{uuid.uuid4().hex}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, path)
        return path
    except OSError as exc:
        log.warning("could not write the commit journal: %s", exc)
        return None


def _commit_output(
    source: str, temp_out: str, plan: EncodePlan, settings: AppSettings,
    info: ffmpeg.MediaInfo,
    source_signature: tuple[int, int] | None = None,
    library_root: str | None = None,
    notes: list[str] | None = None,
) -> str:
    """Put the finished file in its final place, safely.

    The new file is staged next to its destination and flushed to disk before
    the original is touched, and the original stays reachable under a second
    name until the new one has taken its place.  At no point are both gone.
    """
    target = _target_path(source, plan, settings)
    replacing = settings.output.mode == "replace"
    same = os.path.abspath(target) == os.path.abspath(source)
    if replacing and not same and os.path.lexists(target):
        # Film.avi -> Film.mkv next to an unrelated Film.mkv: os.replace would
        # destroy that file without it ever reaching the trash.  (Sidecar and
        # separate-dir targets are our own earlier output and may be replaced.)
        raise FileExistsError(
            f"Ziel {target} existiert bereits und ist nicht die Quelle - nichts ersetzt."
        )
    target_dir = Path(target).parent
    target_dir.mkdir(parents=True, exist_ok=True)

    # Unique per call: two concurrent jobs can share a target name.
    staging = target_dir / f"{STAGING_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}-{Path(target).name}"
    action = settings.output.original_action if replacing else ""
    backup: Path | None = None
    if replacing and same:
        # Where the original stays reachable while its name gets the new file.
        backup = (
            free_backup_path(source) if action == "keep"
            else Path(source).with_name(f"{BACKUP_PREFIX}{uuid.uuid4().hex[:8]}-{Path(source).name}")
        )
    journal = _journal_write({
        "staging": str(staging), "source": source, "target": target,
        "backup": str(backup) if backup else "", "replace": replacing,
        "action": action, "created": time.time(),
    })
    try:
        _stage_and_replace(
            source, temp_out, staging, target, settings,
            expected=source_signature, library_root=library_root, backup=backup, notes=notes,
        )
    except BaseException:
        # Never leave a full-size hidden copy in the library - but only while
        # the original is still in place.  Otherwise the staging copy is one
        # of the two files the library still has.
        if not replacing or os.path.lexists(source):
            staging.unlink(missing_ok=True)
        elif staging.exists():
            log.error(
                "commit of %s failed with the original away from its place - keeping %s "
                "and %s for manual recovery", source, staging, backup,
            )
            journal = None  # the next start rolls it back
        raise
    finally:
        if journal is not None:
            journal.unlink(missing_ok=True)
    return target


def _secure_original(source: str, backup: Path) -> str:
    """Give the original a second name before its own name is reused.

    A hard link costs nothing and keeps the original at ``source`` as well;
    filesystems without links get a rename, which leaves the name empty for the
    blink until the new file arrives.
    """
    try:
        os.link(source, backup)
        return "link"
    except OSError as exc:
        log.debug("hard link for %s failed (%s) - renaming instead", source, exc)
    os.rename(source, backup)
    return "rename"


def _restore_original(source: str, backup: Path) -> None:
    """Put the original back under its name (overwriting a new file there)."""
    os.replace(backup, source)
    _fsync_dir(Path(source).parent)


def _stage_and_replace(
    source: str, temp_out: str, staging: Path, target: str, settings: AppSettings,
    expected: tuple[int, int] | None = None,
    library_root: str | None = None,
    backup: Path | None = None,
    notes: list[str] | None = None,
) -> None:
    # --- 1. stage the new file next to its destination, durable ----------- #
    _ensure_room(staging.parent, os.path.getsize(temp_out), temp_out, "die neue Datei")
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
    _fsync_file(staging)
    _fsync_dir(staging.parent)

    if settings.output.mode != "replace":
        os.replace(str(staging), target)
        _fsync_dir(Path(target).parent)
        return

    # --- 2. everything that can refuse, before the original is touched --- #
    action = settings.output.original_action
    trash_dest = _prepare_trash(source, settings, library_root) if action == "trash" else None
    _check_source_unchanged(source, expected)

    same = os.path.abspath(target) == os.path.abspath(source)
    if same:
        # --- 3a. same name: original keeps a second name, then atomic swap - #
        if backup is None:
            backup = (
                free_backup_path(source) if action == "keep"
                else Path(source).with_name(f"{BACKUP_PREFIX}{uuid.uuid4().hex[:8]}-{Path(source).name}")
            )
        how = _secure_original(source, backup)
        try:
            os.replace(str(staging), target)
        except BaseException:
            if how == "link":
                backup.unlink(missing_ok=True)
            else:
                _restore_original(source, backup)
            raise
        _fsync_dir(Path(target).parent)
        if action == "keep":
            return  # the backup name *is* the kept original
        try:
            if action == "delete":
                os.unlink(backup)
            else:
                _move_to_trash(source, settings, library_root, from_path=str(backup), dest=trash_dest)
        except BaseException as exc:
            # The original could not be put away: bring it back rather than
            # leave it under a hidden name.  The encode is lost, nothing else.
            log.error("could not dispose of the original %s: %s - restoring it", source, exc)
            if os.path.lexists(backup):
                _restore_original(source, backup)
            raise
        return

    # --- 3b. new name (Film.avi -> Film.mkv): place it, then clear the old - #
    os.replace(str(staging), target)
    _fsync_dir(Path(target).parent)
    try:
        if action == "trash":
            _move_to_trash(source, settings, library_root, dest=trash_dest)
        elif action == "delete":
            os.unlink(source)
        else:
            # Kept originals always get the ".original" marker - also when the
            # container changes and the name would not clash.  The scanner skips
            # the marker; without it the kept copy came back as a new candidate
            # on the next scan and was converted again.
            os.rename(source, free_backup_path(source))
    except BaseException as exc:
        if os.path.lexists(source):
            # Original untouched: take the new file back out so the library is
            # exactly as before, instead of holding both side by side.
            log.error("could not dispose of the original %s: %s - undoing", source, exc)
            with contextlib.suppress(OSError):
                os.unlink(target)
            raise
        # The original is gone but the new file is in place - that is a
        # finished conversion with a failed clean-up, not a failure.
        if notes is not None:
            notes.append(f"Original wurde weggeraeumt, Nacharbeit schlug fehl: {exc}")


def recover_interrupted_commits() -> int:
    """Roll back commits a crash or power cut interrupted.  Returns how many.

    Runs at start-up, before the worker.  A journal entry exists only while a
    commit is in flight, and the database never recorded its success - so the
    consistent state is the one before it: the original back under its name,
    our staging copy and safety links gone.  The job itself is re-queued by
    the orphan recovery and simply runs again.
    """
    if not COMMIT_JOURNAL_DIR.is_dir():
        return 0
    handled = 0
    for leftover in COMMIT_JOURNAL_DIR.glob("*.tmp"):
        leftover.unlink(missing_ok=True)
    for entry_path in sorted(COMMIT_JOURNAL_DIR.glob("*.json")):
        try:
            entry = json.loads(entry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entry_path.unlink(missing_ok=True)
            continue
        try:
            done = _roll_back_commit(entry)
        except OSError as exc:
            log.error("could not roll back interrupted commit %s: %s", entry, exc)
            done = False
        if done:
            entry_path.unlink(missing_ok=True)
            handled += 1
    return handled


def _roll_back_commit(entry: dict[str, Any]) -> bool:
    staging = Path(entry.get("staging") or "")
    source = str(entry.get("source") or "")
    target = str(entry.get("target") or "")
    backup = Path(entry["backup"]) if entry.get("backup") else None
    replacing = bool(entry.get("replace"))
    if not staging.name.startswith(STAGING_PREFIX) or not source:
        return True  # not ours - nothing to do

    if backup is not None and os.path.lexists(backup):
        if os.path.lexists(source) and os.path.samefile(source, backup):
            backup.unlink()  # only the safety link was made
        else:
            _restore_original(source, backup)
        log.warning("interrupted commit: %s restored", source)
    if (
        replacing and target and os.path.abspath(target) != os.path.abspath(source)
        and os.path.lexists(target) and not staging.exists() and os.path.lexists(source)
    ):
        # The new file was placed next to an original that is still there.
        os.unlink(target)
        log.warning("interrupted commit: removed unfinished %s", target)
    if staging.exists():
        if replacing and not os.path.lexists(source):
            log.error("interrupted commit: %s is missing - keeping %s", source, staging)
            return False
        staging.unlink()
        log.info("interrupted commit: removed %s", staging)
    return True


def sweep_stale_staging(directories: list[str]) -> int:
    """Remove staging copies from before the commit journal existed.

    Only in the given folders (no walk over the library), only our own prefix,
    and only when the file it was meant to become is present - otherwise the
    staging copy may be the last copy of the new file and is left alone.
    """
    removed = 0
    for directory in sorted(set(directories)):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            match = _STAGING_RE.match(name)
            if not match:
                continue
            path = os.path.join(directory, name)
            if not os.path.lexists(os.path.join(directory, match.group(1))):
                log.warning("leaving %s: the file it belongs to is missing", path)
                continue
            try:
                os.unlink(path)
                removed += 1
            except OSError as exc:
                log.warning("could not remove %s: %s", path, exc)
    return removed


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
        actual_bitrate=float(actual_video),
        actual_vmaf=outcome.vmaf,
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
        if job:
            job.state = JobState.DONE.value
            job.finished_at = utcnow()
            job.progress = 1.0
            job.output_size = outcome.output_size
            job.input_size = outcome.input_size
            job.vmaf = outcome.vmaf
            job.plan = {**plan.to_dict(), **planner.job_markers(job.plan)}
            job.error = ""
        if media:
            media.state = FileState.DONE.value
            media.converted_at = utcnow()
            media.measured_vmaf = outcome.vmaf

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
