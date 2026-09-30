"""Library scanning: walk the disk, probe what changed, analyse candidates."""
from __future__ import annotations

import asyncio
import datetime as dt
import fnmatch
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from sqlalchemy import select, update
from sqlalchemy.orm import load_only

from ..config import AppSettings, TRANSCODE_DIR, load_settings
from ..db import session_scope
from ..models import FileState, HistoryEntry, LibraryPath, MediaFile, ScanRun, utcnow
from . import analyzer, ffmpeg, hwaccel
from .advisor import get_advisor
from .events import bus
from . import maintenance, background

log = logging.getLogger(__name__)

BUSY_MESSAGE = "Es laeuft bereits ein Scan."
CANCELLED_MESSAGE = "Scan abgebrochen"


@dataclass
class ScanState:
    run_id: int | None = None
    running: bool = False
    cancel: asyncio.Event | None = None
    disk_cancel: threading.Event | None = None
    loop: asyncio.AbstractEventLoop | None = None
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


class ScanCancelled(Exception):
    pass


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled(CANCELLED_MESSAGE)


# --------------------------------------------------------------------------- #
# Disk walk
# --------------------------------------------------------------------------- #

#: Our own working folders and files inside a library.  They start with a dot
#: and would be skipped as hidden anyway; naming them keeps that from being an
#: accident of the naming scheme.
OWN_PREFIXES = (".optimizarr-trash", ".optimizarr-staging-")


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


def _storable(path: str) -> bool:
    """Can this path go into the database?

    A name that is not valid UTF-8 reaches Python with surrogate escapes, and
    SQLite refuses to store it - which used to fail the whole scan.
    """
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@dataclass
class WalkReport:
    """What the walk could and could not see - decides what counts as missing."""

    #: Library ids whose root was reachable, with the number of files found.
    found: dict[int, int] = field(default_factory=dict)
    #: Directories that could not be listed; nothing below them is "missing".
    unreadable: list[str] = field(default_factory=list)
    #: Names skipped because they cannot be stored (not UTF-8).
    unstorable: int = 0


def _own_dirs(settings: AppSettings) -> set[str]:
    """Folders Optimizarr writes into - never media, even inside a library."""
    dirs = {os.path.realpath(str(TRANSCODE_DIR))}
    if settings.output.trash_dir.strip():
        dirs.add(os.path.realpath(settings.output.trash_dir.strip()))
    return dirs


def walk_paths(
    roots: list[tuple[int, str]], settings: AppSettings, report: WalkReport | None = None,
    cancel: threading.Event | None = None,
) -> Iterator[tuple[int, str, int, float]]:
    """Yield (library_id, path, size, mtime) for every eligible video file."""
    report = report if report is not None else WalkReport()
    extensions = {f".{e.lower().lstrip('.')}" for e in settings.library.extensions}
    excludes = settings.library.exclude_patterns
    # The codec is unknown until probing, so small files must reach that stage
    # too when migrating H.264. Other codecs still face the normal precheck.
    min_size = (
        0 if settings.analysis.requires_h264_conversion("h264")
        else settings.library.min_file_size_mb * 1024 * 1024
    )
    follow = settings.library.follow_symlinks
    own_dirs = _own_dirs(settings)
    seen_dirs: set[tuple[int, int]] = set()

    def on_error(exc: OSError) -> None:
        log.warning("Ordner nicht lesbar: %s (%s)", exc.filename, exc.strerror)
        if exc.filename:
            report.unreadable.append(str(exc.filename))

    for lib_id, root in roots:
        _check_cancel(cancel)
        if not os.path.isdir(root):
            log.warning("Library path missing: %s", root)
            continue
        unreadable_before = len(report.unreadable)
        count = 0
        for dirpath, dirnames, filenames in os.walk(root, onerror=on_error, followlinks=follow):
            _check_cancel(cancel)
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

            kept = []
            for d in dirnames:
                full_dir = os.path.join(dirpath, d)
                # Hidden folders - this includes our own .optimizarr-trash.
                if d.startswith(".") or d.startswith(OWN_PREFIXES):
                    continue
                if not _storable(full_dir):
                    report.unstorable += 1
                    log.warning(
                        "Ordner mit ungueltigem Namen (kein UTF-8) uebersprungen: %r", full_dir
                    )
                    continue
                if os.path.realpath(full_dir) in own_dirs or _matches_exclude(full_dir, excludes):
                    continue
                kept.append(d)
            dirnames[:] = kept

            for name in filenames:
                _check_cancel(cancel)
                # Hidden files are never media: our own .optimizarr-staging-*
                # leftovers, macOS "._" resource forks.
                if name.startswith(".") or Path(name).stem.endswith(".original"):
                    # ".original" marks an original kept after conversion.
                    continue
                if Path(name).suffix.lower() not in extensions:
                    continue
                full = os.path.join(dirpath, name)
                if not _storable(full):
                    report.unstorable += 1
                    log.warning("Datei mit ungueltigem Namen (kein UTF-8) uebersprungen: %r", full)
                    continue
                if _matches_exclude(full, excludes):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size < min_size:
                    continue
                count += 1
                yield lib_id, full, st.st_size, st.st_mtime

        # The root itself failing to list means it was not really reachable.
        root_norm = root.rstrip("/")
        if not any(p.rstrip("/") == root_norm for p in report.unreadable[unreadable_before:]):
            report.found[lib_id] = report.found.get(lib_id, 0) + count


# --------------------------------------------------------------------------- #
# Database sync
# --------------------------------------------------------------------------- #

#: A write transaction is never held longer than about this while syncing -
#: the encoder and the API need the database too (SQLite has a single writer).
COMMIT_INTERVAL = 1.0

#: Rows gone from disk for longer than this are dropped from the database.
MISSING_RETENTION_DAYS = 30

#: Written after the first full scan that re-probed the library for Dolby
#: Vision.  Probes before that did not recognise DV (see ffmpeg._detect_hdr),
#: so a DV file could sit in the candidate list as plain HDR10 or even SDR.
#: The re-probe is metadata only: a file that turns out not to be DV keeps its
#: state and verdict - no trial encode is spent on it again.
DV_REPROBE_MARKER = "dolby-vision-reprobe.done"
_DV_CODECS = ("hevc", "av1", "h264")


def _dv_marker() -> Path:
    from .. import db

    return db.CONFIG_DIR / DV_REPROBE_MARKER


def _dv_reprobe_pending() -> bool:
    try:
        return not _dv_marker().exists()
    except OSError:
        return False


def _mark_dv_reprobe_done() -> None:
    try:
        path = _dv_marker()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(utcnow().isoformat() + "\n")
    except OSError as exc:
        log.warning("could not write %s: %s", DV_REPROBE_MARKER, exc)


def _dv_reprobe_ids() -> list[int]:
    """Unfinished files probed before DV detection worked."""
    from . import codecs

    with session_scope() as s:
        rows = s.execute(
            select(MediaFile.id, MediaFile.video_codec, MediaFile.hdr_format).where(
                MediaFile.state.in_([
                    FileState.PROBED.value, FileState.CANDIDATE.value, FileState.SKIPPED.value,
                ]),
                MediaFile.ignored.is_(False),
            )
        ).all()
    return [
        file_id for file_id, codec, hdr in rows
        if codecs.normalise(codec) in _DV_CODECS and not ffmpeg.is_dolby_vision(hdr)
    ]


#: Written after the first full scan that re-read the metadata of files already
#: converted.  Encodes before that left the source's codec profile, bit depth,
#: bitrate and streams on the row (the AV1 file is 10-bit, the row said 8).
CONVERTED_REFRESH_MARKER = "converted-metadata-refresh.done"


def _converted_marker() -> Path:
    from .. import db

    return db.CONFIG_DIR / CONVERTED_REFRESH_MARKER


def _converted_refresh_pending() -> bool:
    try:
        return not _converted_marker().exists()
    except OSError:
        return False


def _mark_converted_refresh_done() -> None:
    try:
        path = _converted_marker()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(utcnow().isoformat() + "\n")
    except OSError as exc:
        log.warning("could not write %s: %s", CONVERTED_REFRESH_MARKER, exc)


def _converted_refresh_ids() -> list[int]:
    with session_scope() as s:
        return list(s.execute(
            select(MediaFile.id).where(
                MediaFile.state == FileState.DONE.value,
                MediaFile.converted_at.is_not(None),
            )
        ).scalars().all())


def _refresh_converted(file_id: int, info: ffmpeg.MediaInfo) -> None:
    """Metadata only: a converted file stays "done" with its recorded saving."""
    with session_scope() as s:
        row = s.get(MediaFile, file_id)
        if row is None or row.state != FileState.DONE.value:
            return
        _copy_probe(row, info)


def _restored_state(row: MediaFile) -> str:
    """Where a file that went missing and came back unchanged belongs.

    Size and mtime match, so whatever was known about it still holds - turning
    a converted file into "new" would cost a probe and lose its "done".
    """
    from . import codecs

    if row.ignored:
        return FileState.IGNORED.value
    if row.converted_at is not None and codecs.normalise(row.video_codec) == "av1":
        return FileState.DONE.value
    if row.analyzed_at is not None and row.decision_reason:
        if row.plan and analyzer.reason_means_convert(row.decision_reason):
            return FileState.CANDIDATE.value
        return FileState.SKIPPED.value
    if row.video_codec:
        return FileState.PROBED.value
    return FileState.NEW.value


#: States an ignored file may be in without a job owning it.
_IGNORABLE = (
    FileState.NEW.value, FileState.PROBED.value, FileState.ANALYZING.value,
    FileState.CANDIDATE.value, FileState.SKIPPED.value, FileState.FAILED.value,
    FileState.MISSING.value,
)


def _is_below(path: str, folder: str) -> bool:
    folder = folder.rstrip("/")
    return path == folder or path.startswith(folder + "/")


def _sync_disk_to_db(settings: AppSettings, run_id: int, cancel: threading.Event | None = None) -> tuple[int, int, list[int]]:
    """Upsert everything on disk.  Returns (seen, new, ids_needing_probe).

    The disk is walked first, without touching the database: on a slow or
    sleeping share that takes minutes, and a write transaction held open for
    that long locked out the encoder and the API.  Applying the result is fast
    and commits at least every ``COMMIT_INTERVAL`` seconds.
    """
    with session_scope() as s:
        roots = [
            (lp.id, lp.path)
            for lp in s.execute(select(LibraryPath).where(LibraryPath.enabled.is_(True)))
            .scalars()
            .all()
        ]

    # ---- walk, no database -------------------------------------------------
    report = WalkReport()
    entries: list[tuple[int, str, int, float]] = []
    walk = walk_paths(roots, settings, report, cancel) if cancel is not None else walk_paths(roots, settings, report)
    for entry in walk:
        entries.append(entry)
        if len(entries) % 500 == 0:
            bus.publish("scan.progress", {
                "phase": "walk", "seen": len(entries), "new": 0, "current": entry[1],
            })
    if report.unstorable:
        _log_history(
            "warning", "scan",
            f"{report.unstorable} Datei- oder Ordnernamen sind kein gueltiges UTF-8 und "
            "wurden uebersprungen.",
        )

    # ---- apply ---------------------------------------------------------------
    with session_scope() as s:
        # Compact inventory for missing-file detection. Heavy ORM rows live for
        # only one batch, regardless of the size of the complete library.
        existing = {
            row.path: row for row in s.execute(select(
                MediaFile.id, MediaFile.path, MediaFile.library_id, MediaFile.state,
            ))
        }

        seen_paths: set[str] = set()
        new_count = 0
        needs_probe: list[int] = []
        queued_probe: set[int] = set()

        def want_probe(file_id: int) -> None:
            if file_id not in queued_probe:
                queued_probe.add(file_id)
                needs_probe.append(file_id)

        committed_at = time.monotonic()

        for index, (lib_id, path, size, mtime) in enumerate(entries, 1):
            _check_cancel(cancel)
            if index % 500 == 1:
                paths = [entry[1] for entry in entries[index - 1:index + 499]]
                batch_rows = {row.path: row for row in s.scalars(select(MediaFile).where(MediaFile.path.in_(paths)).options(load_only(
                    MediaFile.id, MediaFile.path, MediaFile.library_id, MediaFile.state,
                    MediaFile.ignored, MediaFile.size, MediaFile.mtime, MediaFile.last_seen, MediaFile.analyzed_at,
                )))}
            seen_paths.add(path)
            row = batch_rows.get(path)
            if row is None:
                # `existing` is a snapshot.  An encode finishing mid-walk renames
                # its row (Film.mp4 -> Film.mkv); inserting that path again broke
                # the UNIQUE constraint and failed the whole scan.
                row = s.execute(
                    select(MediaFile).where(MediaFile.path == path)
                ).scalar_one_or_none()
            if row is None:
                row = MediaFile(
                    path=path, library_id=lib_id, size=size, mtime=mtime,
                    container=Path(path).suffix.lstrip(".").lower(),
                    state=FileState.NEW.value,
                )
                s.add(row)
                s.flush()
                new_count += 1
                want_probe(row.id)
            else:
                _sync_existing(s, row, lib_id, size, mtime, settings, want_probe)

            if index % 500 == 0 or time.monotonic() - committed_at >= COMMIT_INTERVAL:
                run = s.get(ScanRun, run_id)
                if run:
                    run.files_seen = len(seen_paths)
                    run.files_new = new_count
                    run.current_path = path
                s.commit()
                committed_at = time.monotonic()
                bus.publish("scan.progress", {
                    "phase": "walk", "seen": len(seen_paths), "new": new_count, "current": path,
                })
                if index % 500 == 0:
                    s.expunge_all()

        _check_cancel(cancel)
        _mark_missing(s, existing, seen_paths, roots, report)
        s.commit()
        purged = _purge_missing(s)

        run = s.get(ScanRun, run_id)
        if run:
            run.files_seen = len(seen_paths)
            run.files_new = new_count
            run.total = len(needs_probe)
    if purged:
        _log_history(
            "info", "scan",
            f"{purged} Eintraege entfernt, deren Dateien seit ueber "
            f"{MISSING_RETENTION_DAYS} Tagen fehlen.",
        )
    return len(seen_paths), new_count, needs_probe


def _sync_existing(
    s: Any, row: MediaFile, lib_id: int, size: int, mtime: float,
    settings: AppSettings, want_probe: Callable[[int], None],
) -> None:
    """Bring one known row in line with what the walk found."""

    def is_changed() -> bool:
        return abs(row.mtime - mtime) > 1.0 or row.size != size

    if is_changed() or row.state in (FileState.NEW.value, FileState.MISSING.value):
        # Re-read before acting on it: a job may have replaced the file since
        # the snapshot, and the stale row would reset a freshly converted file
        # from "done" to "new".
        s.refresh(row)
    row.last_seen = utcnow()
    row.library_id = lib_id

    if row.state == FileState.ENCODING.value:
        return

    if row.ignored:
        # Ignored stays ignored, changed or not.  Setting it to "new" left it
        # stuck there for good: the probe skips ignored files.
        row.size = size
        row.mtime = mtime
        if row.state in _IGNORABLE:
            row.state = FileState.IGNORED.value
        return

    if row.state == FileState.MISSING.value and not is_changed():
        # Back, and untouched: whatever we knew about it still holds.
        row.state = _restored_state(row)
        row.error = ""
        if row.state in (FileState.NEW.value, FileState.PROBED.value):
            want_probe(row.id)
        return

    if is_changed() or row.state in (FileState.NEW.value, FileState.MISSING.value):
        row.size = size
        row.mtime = mtime
        if row.state == FileState.QUEUED.value:
            # A job owns the file; it probes the file again when it starts.
            return
        row.state = FileState.NEW.value
        row.error = ""
        want_probe(row.id)
    elif row.state == FileState.PROBED.value:
        # Metadata read but never analysed - unfinished work, so it is picked
        # up even when only changed files are rescanned.  This is also how a
        # lifted codec exclusion comes back.
        want_probe(row.id)
    elif not settings.library.rescan_changed_only and row.state in (
        FileState.SKIPPED.value, FileState.CANDIDATE.value
    ):
        want_probe(row.id)
    elif _analysis_is_stale(row, settings):
        want_probe(row.id)


def _mark_missing(
    s: Any, existing: dict[str, MediaFile], seen: set[str],
    roots: list[tuple[int, str]], report: WalkReport,
) -> None:
    """Mark rows as missing - but only where the walk could actually look.

    A disabled library, an unmounted share or an unreadable folder says nothing
    about the files in it.  A reachable root that is completely empty while the
    database knows files there is almost always a share that did not mount (the
    mount point exists, empty), so that is not trusted either.
    """
    root_of = dict(roots)
    known: dict[int, int] = {}
    for row in existing.values():
        if row.library_id is not None and row.state != FileState.MISSING.value:
            known[row.library_id] = known.get(row.library_id, 0) + 1
    trusted: dict[int, str] = {}
    for lib_id, count in report.found.items():
        if count == 0 and known.get(lib_id):
            log.warning(
                "Bibliothek %s ist leer, obwohl %d Dateien bekannt sind - nicht "
                "eingehaengt? Es wird nichts als fehlend markiert.",
                root_of.get(lib_id), known[lib_id],
            )
            continue
        trusted[lib_id] = root_of.get(lib_id, "")

    for path, row in existing.items():
        if path in seen:
            continue
        if not isinstance(row, MediaFile):
            row = s.get(MediaFile, row.id)
            if row is None:
                continue
        s.refresh(row)  # may have been renamed by a finished encode
        if row.path in seen:
            continue
        if row.state in (FileState.MISSING.value, FileState.ENCODING.value):
            continue
        if row.library_id is not None:
            if row.library_id not in trusted:
                continue
        elif not any(_is_below(row.path, root) for root in trusted.values() if root):
            continue
        if any(_is_below(row.path, folder) for folder in report.unreadable):
            continue
        row.state = FileState.MISSING.value


def _purge_missing(s: Any) -> int:
    """Drop rows that have been missing for longer than the retention period.

    Rows with a queued or running job stay - the job references them.  History
    entries and learning samples survive with their reference cleared; the
    file's finished jobs go with it (the foreign key cascades).
    """
    from sqlalchemy import delete as sa_delete

    from ..models import Job, JobState, LearningSample

    cutoff = (utcnow() - dt.timedelta(days=MISSING_RETENTION_DAYS)).replace(tzinfo=None)
    stale = set(s.execute(
        select(MediaFile.id).where(
            MediaFile.state == FileState.MISSING.value, MediaFile.last_seen < cutoff,
        )
    ).scalars())
    if not stale:
        return 0
    busy = set(s.execute(
        select(Job.file_id).where(
            Job.file_id.in_(stale),
            Job.state.in_([JobState.QUEUED.value, JobState.RUNNING.value]),
        )
    ).scalars())
    ids = sorted(stale - busy)
    if not ids:
        return 0
    job_ids = list(s.execute(select(Job.id).where(Job.file_id.in_(ids))).scalars())
    s.execute(update(HistoryEntry).where(HistoryEntry.file_id.in_(ids)).values(file_id=None))
    if job_ids:
        s.execute(
            update(LearningSample).where(LearningSample.job_id.in_(job_ids)).values(job_id=None)
        )
        s.execute(sa_delete(Job).where(Job.id.in_(job_ids)))
    s.execute(sa_delete(MediaFile).where(MediaFile.id.in_(ids)))
    return len(ids)


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


def _copy_probe(row: MediaFile, info: ffmpeg.MediaInfo) -> None:
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


def _store_probe(file_id: int, info: ffmpeg.MediaInfo) -> None:
    with session_scope() as s:
        row = s.get(MediaFile, file_id)
        if row is None:
            return
        _copy_probe(row, info)
        # A queued or running job owns the state; a manual re-analysis turned
        # an encoding file into a candidate and _store_analysis then no longer
        # recognised it as busy.
        if row.state not in (FileState.QUEUED.value, FileState.ENCODING.value, FileState.DONE.value, FileState.IGNORED.value):
            row.state = FileState.PROBED.value
        row.error = ""


def _store_analysis(file_id: int, result: analyzer.AnalysisResult) -> None:
    with session_scope() as s:
        row = s.get(MediaFile, file_id)
        if row is None:
            return
        if row.state == FileState.DONE.value:
            # Realised savings and the completed encode plan remain authoritative.
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
        if row.state not in (FileState.QUEUED.value, FileState.ENCODING.value, FileState.DONE.value, FileState.IGNORED.value):
            row.state = (
                FileState.CANDIDATE.value if result.should_convert else FileState.SKIPPED.value
            )


def _mark_error(file_id: int, message: str) -> None:
    with session_scope() as s:
        row = s.get(MediaFile, file_id)
        if row is None:
            return
        if row.state not in (FileState.DONE.value, FileState.IGNORED.value, FileState.QUEUED.value, FileState.ENCODING.value):
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
    """Full scan: walk -> probe -> analyse.  One at a time.

    Everything after the scan lock is taken sits inside ``try/finally``: an
    exception anywhere - settings, hardware detection, the advisor - must not
    leave ``state.running`` set, or no scan and no queued encode would ever
    start again until a restart.
    """
    if state.running or maintenance.active or background.stopping:
        return {"ok": False, "error": BUSY_MESSAGE}

    cancel = asyncio.Event()
    disk_cancel = threading.Event()
    state.running = True
    state.cancel = cancel
    state.disk_cancel = disk_cancel
    state.loop = asyncio.get_running_loop()
    state.phase = "walk"
    state.done = 0
    state.total = 0
    state.current = ""
    state.started_at = dt.datetime.now(dt.timezone.utc)
    state.run_id = None

    run_id: int | None = None
    probed = analyzed = candidates = errors = 0
    error_message = ""

    try:
        settings = load_settings(force=True)
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

        # ---------------- phase 1: walk ----------------
        dv_reprobe = analyze_only_ids is None and _dv_reprobe_pending()
        converted_refresh = analyze_only_ids is None and _converted_refresh_pending()
        if analyze_only_ids is None:
            work = asyncio.create_task(asyncio.to_thread(_sync_disk_to_db, settings, run_id, disk_cancel))
            try:
                seen, new_count, todo = await asyncio.shield(work)
            except asyncio.CancelledError:
                disk_cancel.set()
                try:
                    await work
                except ScanCancelled:
                    pass
                raise
            _log_history("info", "scan",
                         f"Scan gestartet: {seen} Dateien gefunden, {new_count} neu.")
        else:
            todo = list(analyze_only_ids)
            seen = new_count = 0

        # Once: re-probe what was probed before DV detection worked.  Only a
        # file that turns out to be DV is stored and re-analysed (a precheck
        # skip, no trial encode); every other one keeps its state and verdict.
        dv_only: set[int] = set()
        if dv_reprobe:
            dv_only = set(await asyncio.to_thread(_dv_reprobe_ids)) - set(todo)
            todo = list(todo) + sorted(dv_only)

        if cancel.is_set():
            raise asyncio.CancelledError

        state.phase = "probe"
        state.total = len(todo)
        bus.publish("scan.progress", state.snapshot())

        # ---------------- phase 2: probe ----------------
        probe_sem = asyncio.Semaphore(max(2, settings.analysis.analysis_workers * 2))
        probe_ok: list[tuple[int, ffmpeg.MediaInfo]] = []

        async def probe_one(file_id: int) -> None:
            nonlocal probed, errors
            if cancel.is_set():
                return
            async with probe_sem:
                if cancel.is_set():
                    return
                with session_scope() as s:
                    row = s.get(MediaFile, file_id)
                    path = row.path if row else None
                    ignored = bool(row.ignored) if row else True
                if not path or ignored:
                    return
                state.current = path
                info: ffmpeg.MediaInfo | None = None
                try:
                    info = await ffmpeg.probe(path)
                except ffmpeg.FFmpegError as exc:
                    errors += 1
                    if file_id not in dv_only:   # a DV check alone never fails a file
                        await asyncio.to_thread(_mark_error, file_id, str(exc))
                    log.warning("probe failed for %s: %s", path, exc)
                    await asyncio.to_thread(_log_history, "error", "scan", f"Datei nicht lesbar: {exc}", file_id)
                if info is not None and (
                    file_id not in dv_only or ffmpeg.is_dolby_vision(info.hdr_format)
                ):
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

        # Once: re-read converted files whose rows still describe the source.
        if converted_refresh:
            refresh_ids = await asyncio.to_thread(_converted_refresh_ids)

            async def refresh_one(file_id: int) -> None:
                if cancel.is_set():
                    return
                async with probe_sem:
                    if cancel.is_set():
                        return
                    with session_scope() as s:
                        row = s.get(MediaFile, file_id)
                        path = row.path if row else None
                    if not path:
                        return
                    try:
                        info = await ffmpeg.probe(path)
                    except ffmpeg.FFmpegError as exc:
                        log.warning("metadata refresh failed for %s: %s", path, exc)
                        return
                    await asyncio.to_thread(_refresh_converted, file_id, info)

            await _gather_limited([refresh_one(i) for i in refresh_ids], cancel)
            if cancel.is_set():
                raise asyncio.CancelledError
            await asyncio.to_thread(_mark_converted_refresh_done)

        # ---------------- phase 3: analyse ----------------
        state.phase = "analyze"
        state.total = len(probe_ok)
        state.done = 0
        bus.publish("scan.progress", state.snapshot())

        analyze_sem = asyncio.Semaphore(max(1, settings.analysis.analysis_workers))
        workroot = TRANSCODE_DIR

        async def analyze_one(file_id: int, info: ffmpeg.MediaInfo) -> None:
            nonlocal analyzed, candidates, errors
            if cancel.is_set():
                return
            async with analyze_sem:
                if cancel.is_set():
                    return
                state.current = info.path
                try:
                    result = await analyzer.analyze(
                        info, settings, hw, advisor=advisor, depth=depth,
                        workroot=workroot, cancel_event=cancel,
                    )
                except Exception as exc:  # one bad file must not kill the scan
                    if cancel.is_set():
                        return
                    log.exception("analysis failed for %s", info.path)
                    errors += 1
                    await asyncio.to_thread(_mark_error, file_id, f"Analyse fehlgeschlagen: {exc}")
                    await asyncio.to_thread(_log_history, "error", "scan", f"Analyse fehlgeschlagen: {exc}", file_id)
                    return
                if cancel.is_set():
                    # An analysis cut short (trial encodes terminated, advisor
                    # skipped) is not a verdict - the file keeps its old one.
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
        if cancel.is_set():
            raise asyncio.CancelledError

        if dv_reprobe:
            await asyncio.to_thread(_mark_dv_reprobe_done)

        # ---------------- phase 4: auto-queue ----------------
        if settings.queue.auto_queue_candidates:
            queued = await asyncio.to_thread(_auto_queue, settings)
            if queued:
                _log_history("info", "queue", f"{queued} Dateien automatisch eingereiht.")

        if errors:
            error_message = f"{errors} Datei(en) konnten nicht vollstaendig geprueft werden."
            _safe_history("warning", "scan", error_message)

    except (asyncio.CancelledError, ScanCancelled):
        error_message = CANCELLED_MESSAGE
        _safe_history("warning", "scan", "Scan wurde abgebrochen.")
    except Exception as exc:
        error_message = str(exc) or type(exc).__name__
        log.exception("scan failed")
        _safe_history("error", "scan", f"Scan fehlgeschlagen: {error_message}")
    finally:
        try:
            if run_id is not None:
                with session_scope() as s:
                    run = s.get(ScanRun, run_id)
                    if run:
                        run.state = "cancelled" if error_message == CANCELLED_MESSAGE else (
                            "failed" if error_message else "done"
                        )
                        run.files_probed = probed
                        run.files_analyzed = analyzed
                        run.candidates = candidates
                        run.error = error_message
                        run.finished_at = utcnow()
                        run.current_path = ""
        except Exception:  # the lock below must be released no matter what
            log.exception("could not finalise scan run %s", run_id)
        state.running = False
        state.phase = "idle"
        state.current = ""
        state.cancel = None
        state.disk_cancel = None
        state.loop = None
        bus.publish("scan.finished", {
            "run_id": run_id, "probed": probed, "analyzed": analyzed,
            "candidates": candidates, "error": error_message,
        })
        if _has_pending_analysis() and not background.stopping:
            # Re-evaluations requested while this scan ran.
            asyncio.get_running_loop().call_soon(_start_pending_analysis)

    if not error_message:
        _log_history(
            "success", "scan",
            f"Scan abgeschlossen: {analyzed} Dateien analysiert, {candidates} Kandidaten gefunden.",
        )
    return {
        "ok": not error_message, "run_id": run_id, "probed": probed,
        "analyzed": analyzed, "candidates": candidates, "error": error_message,
    }


def _safe_history(level: str, category: str, message: str) -> None:
    try:
        _log_history(level, category, message)
    except Exception:  # pragma: no cover - the database may be what failed
        log.exception("could not write history entry")


#: How long work that watches the cancel event itself (trial encodes terminate
#: their ffmpeg, the analyzer skips the advisor) gets to wind down before it is
#: cancelled outright - so no ffmpeg process is left behind.
CANCEL_GRACE = 15.0


async def _gather_limited(coros: list[Any], cancel: asyncio.Event) -> None:
    """Run coroutines concurrently, stopping early on cancel.

    Returns as soon as all of them finished or ``cancel`` fired; in the latter
    case whatever is still running gets ``CANCEL_GRACE`` seconds and is then
    cancelled.
    """
    if not coros:
        return
    tasks = [asyncio.create_task(c) for c in coros]
    gathered = asyncio.gather(*tasks, return_exceptions=True)
    stopper = asyncio.create_task(cancel.wait())
    try:
        await asyncio.wait({gathered, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if cancel.is_set() and not gathered.done():
            await asyncio.wait({gathered}, timeout=CANCEL_GRACE)
            for t in tasks:
                if not t.done():
                    t.cancel()
        results = await gathered
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            for failure in failures:
                log.error("scan task failed", exc_info=(type(failure), failure, failure.__traceback__))
            raise RuntimeError(f"{len(failures)} Scan-Aufgabe(n) fehlgeschlagen: {failures[0]}") from failures[0]
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        stopper.cancel()


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
            )
        ).scalars().all()
        for row in rows:
            if (
                not settings.analysis.requires_h264_conversion(row.video_codec)
                and row.estimated_saving_pct < threshold
            ):
                continue
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
        if state.disk_cancel is not None:
            state.disk_cancel.set()
        if state.loop is not None and state.loop.is_running():
            try:
                same_loop = asyncio.get_running_loop() is state.loop
            except RuntimeError:
                same_loop = False
            if same_loop:
                state.cancel.set()
            else:
                state.loop.call_soon_threadsafe(state.cancel.set)
        else:
            state.cancel.set()
        return True
    return False


# --------------------------------------------------------------------------- #
# Re-evaluation after a settings change
# --------------------------------------------------------------------------- #

#: Trigger name of the analysis run a settings change starts.
SETTINGS_TRIGGER = "settings"

_pending_analysis: set[int] = set()
_pending_lock = threading.Lock()
_background: set[asyncio.Task[Any]] = set()


def _has_pending_analysis() -> bool:
    with _pending_lock:
        return bool(_pending_analysis)


def request_analysis(file_ids: list[int]) -> bool:
    """Analyse these files soon, without waiting for the next scheduled scan.

    Safe to call from any thread: the settings endpoint runs in FastAPI's
    threadpool, while the scan has to run on the event loop.  When a scan is
    already running the files are taken up right after it.  Returns False when
    there is no event loop to run on - the files then stay in ``probed`` and
    the next scan picks them up.
    """
    ids = {int(i) for i in file_ids}
    if not ids:
        return False
    with _pending_lock:
        _pending_analysis.update(ids)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        running.call_soon(_start_pending_analysis)
        return True
    loop = bus._loop
    if loop is None or loop.is_closed() or not loop.is_running():
        return False
    loop.call_soon_threadsafe(_start_pending_analysis)
    return True


def _start_pending_analysis() -> None:
    """On the event loop: start the analysis run for everything requested."""
    if state.running or background.stopping:
        return  # run_scan comes back here when it finishes
    with _pending_lock:
        ids = sorted(_pending_analysis)
        _pending_analysis.clear()
    if not ids:
        return
    task = background.spawn(_run_pending(ids), "optimizarr-settings-analysis")
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _run_pending(ids: list[int]) -> None:
    result = await run_scan(trigger=SETTINGS_TRIGGER, analyze_only_ids=ids)
    if not result.get("ok") and result.get("error") == BUSY_MESSAGE:
        # Another scan got the lock first; it hands back to us when done.
        with _pending_lock:
            _pending_analysis.update(ids)


def apply_h264_conversion_change(before: bool, after: bool) -> int:
    """Re-evaluate stored decisions once when the migration mode changes."""
    from . import codecs

    if before == after:
        return 0
    ids: list[int] = []
    with session_scope() as s:
        rows = s.execute(select(MediaFile).where(
            MediaFile.state.in_([FileState.CANDIDATE.value, FileState.SKIPPED.value]),
            MediaFile.ignored.is_(False),
        )).scalars().all()
        for row in rows:
            if codecs.normalise(row.video_codec) != "h264":
                continue
            row.state = FileState.PROBED.value
            row.analyzed_at = None
            row.decision_reason = "H.264-Modus geaendert - wird neu bewertet."
            row.plan = None
            row.estimated_size = 0
            row.estimated_saving_bytes = 0
            row.estimated_saving_pct = 0.0
            ids.append(row.id)
    if ids:
        bus.publish("library.changed", {"h264_reanalysis": len(ids)})
        request_analysis(ids)
    return len(ids)


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

    *No longer excluded* files go back to ``probed`` and an analysis run for
    exactly those files starts right away (see ``request_analysis``) instead of
    waiting for the next scheduled scan.  Only files skipped *by this setting*
    are touched: one that was skipped for being tiny or already lean stays
    skipped, and no trial encode is spent re-discovering that.
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

    restored_ids: list[int] = []
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
                restored_ids.append(row.id)
            result["restored"] = len(restored_ids)

    if result["excluded"] or result["restored"]:
        bits = []
        if result["excluded"]:
            bits.append(f"{result['excluded']} Dateien aus der Kandidatenliste entfernt")
        if result["restored"]:
            bits.append(f"{result['restored']} Dateien werden neu bewertet")
        _log_history("info", "settings", "Codec-Ausschluss geaendert: " + ", ".join(bits))
        bus.publish("library.changed", {"codec_exclusions": result})
    if restored_ids:
        result["analysis_started"] = request_analysis(restored_ids)
    return result
