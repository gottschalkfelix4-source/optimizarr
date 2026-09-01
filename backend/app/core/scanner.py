"""Library scanning: walk the disk, probe what changed, analyse candidates."""
from __future__ import annotations

import asyncio
import datetime as dt
import fnmatch
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select, update

from ..config import AppSettings, TRANSCODE_DIR, load_settings
from ..db import session_scope
from ..models import FileState, HistoryEntry, LibraryPath, MediaFile, ScanRun, utcnow
from . import analyzer, ffmpeg, hwaccel
from .advisor import get_advisor
from .events import bus

log = logging.getLogger(__name__)


@dataclass
class ScanState:
    run_id: int | None = None
    running: bool = False
    cancel: asyncio.Event | None = None
    phase: str = "idle"
    total: int = 0
    done: int = 0
    current: str = ""
    started_at: dt.datetime | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "running": self.running,
            "phase": self.phase,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "progress": (self.done / self.total) if self.total else 0.0,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }


state = ScanState()


# --------------------------------------------------------------------------- #
# Disk walk
# --------------------------------------------------------------------------- #

def _matches_exclude(path: str, patterns: list[str]) -> bool:
    normalised = path.replace("\\", "/").lower()
    for pattern in patterns:
        p = pattern.strip().lower()
        if not p:
            continue
        if fnmatch.fnmatch(normalised, p):
            return True
        # Bare fragments like "sample" should also match anywhere in the path.
        if "*" not in p and "?" not in p and p in normalised:
            return True
    return False


def walk_paths(
    roots: list[tuple[int, str]], settings: AppSettings
) -> Iterator[tuple[int, str, int, float]]:
    """Yield (library_id, path, size, mtime) for every eligible video file."""
    extensions = {f".{e.lower().lstrip('.')}" for e in settings.library.extensions}
    excludes = settings.library.exclude_patterns
    min_size = settings.library.min_file_size_mb * 1024 * 1024
    follow = settings.library.follow_symlinks
    seen_dirs: set[tuple[int, int]] = set()

    for lib_id, root in roots:
        if not os.path.isdir(root):
            log.warning("Library path missing: %s", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow):
            # Guard against symlink loops when following is enabled.
            if follow:
                try:
                    st = os.stat(dirpath)
                    key = (st.st_dev, st.st_ino)
                    if key in seen_dirs:
                        dirnames[:] = []
                        continue
                    seen_dirs.add(key)
                except OSError:
                    continue

            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and not _matches_exclude(os.path.join(dirpath, d), excludes)
            ]
            for name in filenames:
                if Path(name).suffix.lower() not in extensions:
                    continue
                full = os.path.join(dirpath, name)
                if _matches_exclude(full, excludes):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size < min_size:
                    continue
                yield lib_id, full, st.st_size, st.st_mtime


# --------------------------------------------------------------------------- #
# Database sync
# --------------------------------------------------------------------------- #

def _sync_disk_to_db(settings: AppSettings, run_id: int) -> tuple[int, int, list[int]]:
    """Upsert everything on disk.  Returns (seen, new, ids_needing_probe)."""
    with session_scope() as s:
        roots = [
            (lp.id, lp.path)
            for lp in s.execute(select(LibraryPath).where(LibraryPath.enabled.is_(True)))
            .scalars()
            .all()
        ]
        existing: dict[str, MediaFile] = {
            mf.path: mf for mf in s.execute(select(MediaFile)).scalars().all()
        }

        seen_paths: set[str] = set()
        new_count = 0
        needs_probe: list[int] = []
        batch = 0

        for lib_id, path, size, mtime in walk_paths(roots, settings):
            seen_paths.add(path)
            row = existing.get(path)
            if row is None:
                row = MediaFile(
                    path=path, library_id=lib_id, size=size, mtime=mtime,
                    container=Path(path).suffix.lstrip(".").lower(),
                    state=FileState.NEW.value,
                )
                s.add(row)
                s.flush()
                new_count += 1
                needs_probe.append(row.id)
            else:
                row.last_seen = utcnow()
                row.library_id = lib_id
                changed = (
                    abs(row.mtime - mtime) > 1.0
                    or row.size != size
                    or row.state in (FileState.NEW.value, FileState.MISSING.value)
                )
                if changed and row.state != FileState.ENCODING.value:
                    row.size = size
                    row.mtime = mtime
                    row.state = FileState.NEW.value
                    row.error = ""
                    needs_probe.append(row.id)
                elif row.state == FileState.PROBED.value:
                    # Metadata read but never analysed - unfinished work, so it
                    # is picked up even when only changed files are rescanned.
                    # This is also how a lifted codec exclusion comes back.
                    needs_probe.append(row.id)
                elif not settings.library.rescan_changed_only and row.state in (
                    FileState.SKIPPED.value, FileState.CANDIDATE.value
                ):
                    needs_probe.append(row.id)
                elif _analysis_is_stale(row, settings):
                    needs_probe.append(row.id)

            batch += 1
            if batch % 500 == 0:
                s.flush()
                run = s.get(ScanRun, run_id)
                if run:
                    run.files_seen = len(seen_paths)
                    run.files_new = new_count
                    run.current_path = path
                s.commit()
                bus.publish("scan.progress", {
                    "phase": "walk", "seen": len(seen_paths), "new": new_count, "current": path,
                })

        # Anything in the DB we did not see is gone from disk.
        for path, row in existing.items():
            if path in seen_paths:
                continue
            if row.state not in (FileState.MISSING.value, FileState.ENCODING.value):
                row.state = FileState.MISSING.value

        run = s.get(ScanRun, run_id)
        if run:
            run.files_seen = len(seen_paths)
            run.files_new = new_count
            run.total = len(needs_probe)
        return len(seen_paths), new_count, needs_probe


def _analysis_is_stale(row: MediaFile, settings: AppSettings) -> bool:
    days = settings.library.reanalyze_after_days
    if not days or row.analyzed_at is None:
        return False
    if row.state not in (FileState.CANDIDATE.value, FileState.SKIPPED.value):
        return False
    analyzed = row.analyzed_at
    if analyzed.tzinfo is None:
        analyzed = analyzed.replace(tzinfo=dt.timezone.utc)
    return (utcnow() - analyzed).days >= days


def _store_probe(file_id: int, info: ffmpeg.MediaInfo) -> None:
    with session_scope() as s:
        row = s.get(MediaFile, file_id)
        if row is None:
            return
        row.container = info.container or row.container
        row.video_codec = info.video_codec
        row.profile = info.profile
        row.width = info.width
        row.height = info.height
        row.fps = info.fps
        row.duration = info.duration
        row.video_bitrate = info.video_bitrate
        row.bit_depth = info.bit_depth
        row.pix_fmt = info.pix_fmt
        row.is_hdr = info.is_hdr
        row.hdr_format = info.hdr_format
        row.color_primaries = info.color_primaries
        row.color_transfer = info.color_transfer
        row.color_space = info.color_space
        row.interlaced = info.interlaced
        row.audio_streams = info.audio_streams
        row.subtitle_streams = info.subtitle_streams
        row.state = FileState.PROBED.value
        row.error = ""


def _store_analysis(file_id: int, result: analyzer.AnalysisResult) -> None:
    with session_scope() as s:
        row = s.get(MediaFile, file_id)
        if row is None:
            return
        row.estimated_size = result.estimated_size
        row.estimated_saving_bytes = max(0, result.estimated_saving_bytes)
        row.estimated_saving_pct = result.estimated_saving_pct
        row.confidence = result.confidence
        row.decision_reason = result.reason
        row.analysis_depth = result.depth
        row.analyzed_at = utcnow()
        row.plan = result.plan.to_dict() if result.plan else None
        if result.advice is not None and result.advice.ok:
            row.advisor_note = result.advice.reasoning
        if row.state not in (FileState.QUEUED.value, FileState.ENCODING.value):
            row.state = (
                FileState.CANDIDATE.value if result.should_convert else FileState.SKIPPED.value
            )


def _mark_error(file_id: int, message: str) -> None:
    with session_scope() as s:
        row = s.get(MediaFile, file_id)
        if row is None:
            return
        row.state = FileState.FAILED.value
        row.error = message[:2000]


def _log_history(level: str, category: str, message: str, file_id: int | None = None,
                 detail: dict[str, Any] | None = None) -> None:
    with session_scope() as s:
        s.add(HistoryEntry(level=level, category=category, message=message,
                           file_id=file_id, detail=detail))
    bus.publish("history", {"level": level, "category": category, "message": message})


# --------------------------------------------------------------------------- #
# Scan orchestration
# --------------------------------------------------------------------------- #

async def run_scan(trigger: str = "manual", analyze_only_ids: list[int] | None = None,
                   depth: str | None = None) -> dict[str, Any]:
    """Full scan: walk -> probe -> analyse.  One at a time."""
    if state.running:
        return {"ok": False, "error": "Es laeuft bereits ein Scan."}

    settings = load_settings(force=True)
    cancel = asyncio.Event()
    state.running = True
    state.cancel = cancel
    state.phase = "walk"
    state.done = 0
    state.total = 0
    state.current = ""
    state.started_at = dt.datetime.now(dt.timezone.utc)

    with session_scope() as s:
        run = ScanRun(trigger=trigger, state="running")
        s.add(run)
        s.flush()
        run_id = run.id
    state.run_id = run_id
    bus.publish("scan.started", {"run_id": run_id, "trigger": trigger})

    hw = hwaccel.cached()
    if hw is None:
        hw = await hwaccel.detect(
            settings.hardware.render_device, settings.hardware.qsv_low_power
        )
    advisor = get_advisor(settings.advisor)
    advisor.reset_budget()

    probed = analyzed = candidates = 0
    error_message = ""

    try:
        # ---------------- phase 1: walk ----------------
        if analyze_only_ids is None:
            seen, new_count, todo = await asyncio.to_thread(_sync_disk_to_db, settings, run_id)
            _log_history("info", "scan",
                         f"Scan gestartet: {seen} Dateien gefunden, {new_count} neu.")
        else:
            todo = list(analyze_only_ids)
            seen = new_count = 0

        if cancel.is_set():
            raise asyncio.CancelledError

        state.phase = "probe"
        state.total = len(todo)
        bus.publish("scan.progress", state.snapshot())

        # ---------------- phase 2: probe ----------------
        probe_sem = asyncio.Semaphore(max(2, settings.analysis.analysis_workers * 2))
        probe_ok: list[tuple[int, ffmpeg.MediaInfo]] = []

        async def probe_one(file_id: int) -> None:
            nonlocal probed
            if cancel.is_set():
                return
            async with probe_sem:
                with session_scope() as s:
                    row = s.get(MediaFile, file_id)
                    path = row.path if row else None
                    ignored = bool(row.ignored) if row else True
                if not path or ignored:
                    return
                state.current = path
                try:
                    info = await ffmpeg.probe(path)
                except ffmpeg.FFmpegError as exc:
                    await asyncio.to_thread(_mark_error, file_id, str(exc))
                    log.warning("probe failed for %s: %s", path, exc)
                else:
                    await asyncio.to_thread(_store_probe, file_id, info)
                    probe_ok.append((file_id, info))
                probed += 1
                state.done = probed
                if probed % 5 == 0 or probed == state.total:
                    bus.publish("scan.progress", state.snapshot())

        await _gather_limited([probe_one(i) for i in todo], cancel)
        if cancel.is_set():
            raise asyncio.CancelledError

        with session_scope() as s:
            run = s.get(ScanRun, run_id)
            if run:
                run.files_probed = probed

        # ---------------- phase 3: analyse ----------------
        state.phase = "analyze"
        state.total = len(probe_ok)
        state.done = 0
        bus.publish("scan.progress", state.snapshot())

        analyze_sem = asyncio.Semaphore(max(1, settings.analysis.analysis_workers))
        workroot = TRANSCODE_DIR

        async def analyze_one(file_id: int, info: ffmpeg.MediaInfo) -> None:
            nonlocal analyzed, candidates
            if cancel.is_set():
                return
            async with analyze_sem:
                state.current = info.path
                try:
                    result = await analyzer.analyze(
                        info, settings, hw, advisor=advisor, depth=depth,
                        workroot=workroot, cancel_event=cancel,
                    )
                except Exception as exc:  # one bad file must not kill the scan
                    log.exception("analysis failed for %s", info.path)
                    await asyncio.to_thread(_mark_error, file_id, f"Analyse fehlgeschlagen: {exc}")
                    return
                await asyncio.to_thread(_store_analysis, file_id, result)
                analyzed += 1
                if result.should_convert:
                    candidates += 1
                state.done = analyzed
                bus.publish("scan.progress", {
                    **state.snapshot(), "candidates": candidates,
                })
                bus.publish("file.analyzed", {
                    "file_id": file_id,
                    "path": info.path,
                    "decision": result.decision,
                    "saving_bytes": result.estimated_saving_bytes,
                    "saving_pct": round(result.estimated_saving_pct, 1),
                })

        await _gather_limited([analyze_one(fid, info) for fid, info in probe_ok], cancel)

        # ---------------- phase 4: auto-queue ----------------
        if settings.queue.auto_queue_candidates and not cancel.is_set():
            queued = await asyncio.to_thread(_auto_queue, settings)
            if queued:
                _log_history("info", "queue", f"{queued} Dateien automatisch eingereiht.")

    except asyncio.CancelledError:
        error_message = "Scan abgebrochen"
        _log_history("warning", "scan", "Scan wurde abgebrochen.")
    except Exception as exc:  # pragma: no cover - defensive
        error_message = str(exc)
        log.exception("scan failed")
        _log_history("error", "scan", f"Scan fehlgeschlagen: {exc}")
    finally:
        with session_scope() as s:
            run = s.get(ScanRun, run_id)
            if run:
                run.state = "cancelled" if error_message == "Scan abgebrochen" else (
                    "failed" if error_message else "done"
                )
                run.files_probed = probed
                run.files_analyzed = analyzed
                run.candidates = candidates
                run.error = error_message
                run.finished_at = utcnow()
                run.current_path = ""
        state.running = False
        state.phase = "idle"
        state.current = ""
        state.cancel = None
        bus.publish("scan.finished", {
            "run_id": run_id, "probed": probed, "analyzed": analyzed,
            "candidates": candidates, "error": error_message,
        })

    if not error_message:
        _log_history(
            "success", "scan",
            f"Scan abgeschlossen: {analyzed} Dateien analysiert, {candidates} Kandidaten gefunden.",
        )
    return {
        "ok": not error_message, "run_id": run_id, "probed": probed,
        "analyzed": analyzed, "candidates": candidates, "error": error_message,
    }


async def _gather_limited(coros: list[Any], cancel: asyncio.Event) -> None:
    """Run coroutines concurrently, stopping early on cancel."""
    if not coros:
        return
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if cancel.is_set():
            for t in tasks:
                if not t.done():
                    t.cancel()


def _auto_queue(settings: AppSettings) -> int:
    """Queue candidates that clear the auto-queue threshold."""
    from ..models import Job, JobState

    threshold = settings.queue.auto_queue_min_saving_percent
    queued = 0
    with session_scope() as s:
        rows = s.execute(
            select(MediaFile).where(
                MediaFile.state == FileState.CANDIDATE.value,
                MediaFile.ignored.is_(False),
                MediaFile.estimated_saving_pct >= threshold,
            )
        ).scalars().all()
        for row in rows:
            active = s.execute(
                select(Job).where(
                    Job.file_id == row.id,
                    Job.state.in_([JobState.QUEUED.value, JobState.RUNNING.value]),
                )
            ).scalars().first()
            if active:
                continue
            s.add(Job(
                file_id=row.id, plan=row.plan, input_size=row.size,
                predicted_size=row.estimated_size,
                priority=100 - min(99, int(row.estimated_saving_pct)),
            ))
            row.state = FileState.QUEUED.value
            queued += 1
    if queued:
        bus.publish("queue.changed", {"queued": queued})
    return queued


def cancel_scan() -> bool:
    if state.running and state.cancel is not None:
        state.cancel.set()
        return True
    return False


# --------------------------------------------------------------------------- #
# Codec exclusions
# --------------------------------------------------------------------------- #

def apply_codec_exclusions(before: list[str], after: list[str]) -> dict[str, Any]:
    """Bring the stored library in line with a changed exclusion list.

    Without this the setting would only apply to files analysed *after* the
    change - the HEVC files already sitting in the candidate list would stay
    there, which is precisely the list the user was trying to clean up.

    Both directions are handled, and neither throws work away:

    *Newly excluded* candidates become skipped and lose their estimate, so the
    dashboard totals stop promising savings nobody intends to collect.  Files
    that are queued or already encoding are left alone - somebody put them
    there on purpose - but they are counted, so the UI can say so.

    *No longer excluded* files go back to ``probed`` and get re-analysed on the
    next scan.  Only files skipped *by this setting* are touched: one that was
    skipped for being tiny or already lean stays skipped, and no trial encode
    is spent re-discovering that.
    """
    from . import codecs

    old = set(codecs.normalise_list(before))
    new = set(codecs.normalise_list(after))
    added = new - old
    removed = old - new
    # Match every spelling a probe may have stored, not just the canonical one.
    added_spellings = [s for c in added for s in codecs.spellings(c)]
    removed_spellings = [s for c in removed for s in codecs.spellings(c)]
    result: dict[str, Any] = {
        "added": sorted(added), "removed": sorted(removed),
        "excluded": 0, "restored": 0, "queued_untouched": 0,
    }
    if not added and not removed:
        return result

    with session_scope() as s:
        if added:
            rows = s.execute(
                select(MediaFile).where(
                    MediaFile.video_codec.in_(added_spellings),
                    MediaFile.state.in_([
                        FileState.CANDIDATE.value, FileState.PROBED.value,
                        FileState.QUEUED.value, FileState.ENCODING.value,
                    ]),
                )
            ).scalars().all()
            for row in rows:
                if row.state in (FileState.QUEUED.value, FileState.ENCODING.value):
                    result["queued_untouched"] += 1
                    continue
                row.state = FileState.SKIPPED.value
                row.decision_reason = f"{codecs.label(row.video_codec)} {codecs.EXCLUSION_REASON}"
                row.estimated_size = 0
                row.estimated_saving_bytes = 0
                row.estimated_saving_pct = 0.0
                row.plan = None
                result["excluded"] += 1

        if removed:
            rows = s.execute(
                select(MediaFile).where(
                    MediaFile.video_codec.in_(removed_spellings),
                    MediaFile.state == FileState.SKIPPED.value,
                    MediaFile.decision_reason.like(f"%{codecs.EXCLUSION_REASON}"),
                )
            ).scalars().all()
            for row in rows:
                row.state = FileState.PROBED.value
                row.decision_reason = ""
                row.analyzed_at = None
                result["restored"] += 1

    if result["excluded"] or result["restored"]:
        bits = []
        if result["excluded"]:
            bits.append(f"{result['excluded']} Dateien aus der Kandidatenliste entfernt")
        if result["restored"]:
            bits.append(f"{result['restored']} Dateien zur Neubewertung vorgemerkt")
        _log_history("info", "settings", "Codec-Ausschluss geaendert: " + ", ".join(bits))
        bus.publish("library.changed", {"codec_exclusions": result})
    return result
