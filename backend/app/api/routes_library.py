"""Library paths, file listing, scanning."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import security
from ..config import DEFAULT_MEDIA_ROOT, TRANSCODE_DIR, load_settings
from ..core import analyzer, codecs, ffmpeg, hwaccel, planner, scanner, worker
from ..core.advisor import get_advisor
from ..core.events import bus
from ..db import get_session, session_scope
from ..models import FileState, Job, JobState, LibraryPath, MediaFile, ScanRun, utcnow
from . import serializers

log = logging.getLogger(__name__)
router = APIRouter()


# --------------------------------------------------------------------------- #
# Library paths
# --------------------------------------------------------------------------- #

class LibraryPathIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    name: str = Field("", max_length=255)
    enabled: bool = True


class LibraryPathPatch(BaseModel):
    """What can change on an existing library.  The path itself cannot - that
    would silently orphan every file row below the old one.

    ``LibraryPath.profile`` is no longer offered: nothing ever read it, and a
    per-library profile would need the planner to take it into account.  The
    column stays in SQLite so existing databases need no migration.
    """

    name: str | None = Field(None, max_length=255)
    enabled: bool | None = None


@router.get("/library/paths")
def list_paths(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = session.execute(select(LibraryPath).order_by(LibraryPath.id)).scalars().all()
    out = []
    for row in rows:
        counts = session.execute(
            select(MediaFile.state, func.count(MediaFile.id), func.sum(MediaFile.size))
            .where(MediaFile.library_id == row.id)
            .group_by(MediaFile.state)
        ).all()
        total = sum(c for _, c, _ in counts)
        size = sum(s or 0 for _, _, s in counts)
        by_state = {state: c for state, c, _ in counts}
        out.append(serializers.library_path(row, {
            "file_count": total,
            "total_size": size,
            "candidates": by_state.get(FileState.CANDIDATE.value, 0),
            "converted": by_state.get(FileState.DONE.value, 0),
            "exists": os.path.isdir(row.path),
        }))
    return out


@router.post("/library/paths")
def add_path(payload: LibraryPathIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    raw = payload.path.strip()
    if not raw.startswith("/"):
        raise HTTPException(status_code=422, detail="Bitte einen absoluten Pfad angeben (beginnend mit /).")
    path = os.path.normpath(raw)
    if security.is_system_path(path) or security.is_system_path(os.path.realpath(path)):
        raise HTTPException(
            status_code=422,
            detail=f"'{path}' ist das Wurzelverzeichnis oder ein Systemordner und kann "
                   "keine Bibliothek sein. Bitte den Medienordner waehlen, z. B. /media/Filme.",
        )
    if not os.path.isdir(path):
        raise HTTPException(
            status_code=400,
            detail=f"Der Pfad '{path}' existiert im Container nicht. "
                   "Ist er im Docker-Template als Volume gemappt?",
        )
    existing = session.execute(
        select(LibraryPath).where(LibraryPath.path == path)
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Dieser Pfad ist bereits eingetragen.")
    row = LibraryPath(path=path, name=payload.name or Path(path).name, enabled=payload.enabled)
    session.add(row)
    session.commit()
    bus.publish("library.changed", {"action": "added", "path": path})
    return serializers.library_path(row)


@router.patch("/library/paths/{path_id}")
def update_path(
    path_id: int, payload: LibraryPathPatch, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = session.get(LibraryPath, path_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Pfad nicht gefunden")
    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.enabled is not None:
        row.enabled = payload.enabled
    session.commit()
    bus.publish("library.changed", {"action": "updated", "path": row.path})
    return serializers.library_path(row)


@router.delete("/library/paths/{path_id}")
def delete_path(
    path_id: int, keep_files: bool = False, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Remove a library.  Its jobs go first: a running encode is cancelled and
    queued ones are dropped, so nothing keeps working on a library that is gone."""
    row = session.get(LibraryPath, path_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Pfad nicht gefunden")
    path = row.path
    file_ids = select(MediaFile.id).where(MediaFile.library_id == path_id)
    jobs = session.execute(
        select(Job).where(
            Job.file_id.in_(file_ids),
            Job.state.in_([JobState.QUEUED.value, JobState.RUNNING.value]),
        )
    ).scalars().all()
    cancelled = removed = 0
    for job in jobs:
        media = session.get(MediaFile, job.file_id)
        restore = (job.plan or {}).get(planner.RESTORE_STATE) or FileState.CANDIDATE.value
        if job.state == JobState.RUNNING.value:
            if not worker.queue_worker.cancel_job(job.id):
                # Running in the DB only: a restart lost the process.
                job.state = JobState.CANCELLED.value
                job.finished_at = utcnow()
            cancelled += 1
        else:
            session.delete(job)
            removed += 1
        if media is not None and media.state in (FileState.QUEUED.value, FileState.ENCODING.value):
            media.state = restore
    session.flush()
    files = session.query(MediaFile).filter(MediaFile.library_id == path_id)
    if keep_files:
        files.update({MediaFile.library_id: None}, synchronize_session=False)
    else:
        files.delete(synchronize_session=False)
    session.delete(row)
    session.commit()
    if jobs:
        bus.publish("queue.changed", {})
    bus.publish("library.changed", {"action": "removed", "path": path})
    return {"ok": True, "jobs_cancelled": cancelled, "jobs_removed": removed}


@router.get("/library/browse")
def browse(path: str = Query(default="")) -> dict[str, Any]:
    """Directory picker for the settings screen - container-side paths only."""
    target = Path(path or DEFAULT_MEDIA_ROOT)
    if not target.is_absolute():
        target = Path("/") / target
    if not target.is_dir():
        # Falling back to "/" made a typo look like a valid choice.
        raise HTTPException(
            status_code=404, detail=f"Der Ordner '{target}' existiert im Container nicht."
        )
    entries: list[dict[str, Any]] = []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    entries.append({
                        "name": entry.name,
                        "path": str(entry),
                        "readable": os.access(str(entry), os.R_OK),
                    })
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Kein Zugriff auf {target}")
    return {
        "path": str(target),
        "parent": str(target.parent) if str(target) != "/" else None,
        "entries": entries[:500],
    }


@router.get("/library/codecs")
def library_codecs(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Which video codecs the library actually contains, and how much of each.

    The exclusion setting used to be a free-text field, which meant guessing
    ffprobe's spelling and getting no feedback when the guess was wrong.  With
    this the settings screen can list what is really there, with the file
    counts that make the decision obvious.
    """
    rows = session.execute(
        select(
            MediaFile.video_codec,
            func.count(MediaFile.id),
            func.sum(MediaFile.size),
            func.sum(
                case((MediaFile.state == FileState.CANDIDATE.value, 1), else_=0)
            ),
        )
        .where(MediaFile.video_codec != "")
        .group_by(MediaFile.video_codec)
    ).all()

    excluded = load_settings().analysis.skip_codecs
    merged: dict[str, dict[str, Any]] = {}
    for raw, count, size, candidates in rows:
        canonical = codecs.normalise(raw)
        entry = merged.setdefault(canonical, {
            "codec": canonical,
            "label": codecs.label(canonical),
            "files": 0,
            "total_size": 0,
            "candidates": 0,
            "excluded": codecs.is_excluded(canonical, excluded),
        })
        entry["files"] += count or 0
        entry["total_size"] += size or 0
        entry["candidates"] += candidates or 0

    # Codecs that are excluded but no longer present must stay visible, or the
    # only way to remove them would be to know they are there.
    for name in excluded:
        canonical = codecs.normalise(name)
        if canonical and canonical not in merged:
            merged[canonical] = {
                "codec": canonical, "label": codecs.label(canonical),
                "files": 0, "total_size": 0, "candidates": 0, "excluded": True,
            }

    items = sorted(merged.values(), key=lambda e: (-e["files"], e["label"]))
    return {"items": items, "known": [
        {"codec": c, "label": label} for c, label in codecs.LABELS.items()
    ]}


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #

@router.get("/files")
def list_files(
    session: Session = Depends(get_session),
    state: str | None = None,
    library_id: int | None = None,
    search: str | None = None,
    codec: str | None = None,
    sort: Literal[
        "saving", "size", "name", "saving_pct", "analyzed", "duration"
    ] = "saving",
    direction: Literal["asc", "desc"] = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    query = select(MediaFile)
    count_query = select(func.count(MediaFile.id))

    conditions = []
    if state and state != "all":
        if state == "actionable":
            conditions.append(MediaFile.state.in_([
                FileState.CANDIDATE.value, FileState.QUEUED.value, FileState.ENCODING.value,
            ]))
        else:
            conditions.append(MediaFile.state == state)
    if library_id:
        conditions.append(MediaFile.library_id == library_id)
    if codec:
        conditions.append(MediaFile.video_codec.in_(codecs.spellings(codec)))
    if search:
        # % and _ are wildcards in LIKE - a search for "S01_E01" means the text.
        escaped = (
            search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        conditions.append(func.lower(MediaFile.path).like(f"%{escaped}%", escape="\\"))
    for cond in conditions:
        query = query.where(cond)
        count_query = count_query.where(cond)

    sort_columns = {
        "saving": MediaFile.estimated_saving_bytes,
        "saving_pct": MediaFile.estimated_saving_pct,
        "size": MediaFile.size,
        "name": MediaFile.path,
        "analyzed": MediaFile.analyzed_at,
        "duration": MediaFile.duration,
    }
    column = sort_columns.get(sort, MediaFile.estimated_saving_bytes)
    query = query.order_by(column.desc() if direction == "desc" else column.asc())

    total = session.execute(count_query).scalar() or 0
    rows = session.execute(
        query.offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()

    aggregates = session.execute(
        select(
            func.count(MediaFile.id),
            func.sum(MediaFile.size),
            func.sum(MediaFile.estimated_saving_bytes),
        ).where(*conditions) if conditions else
        select(
            func.count(MediaFile.id),
            func.sum(MediaFile.size),
            func.sum(MediaFile.estimated_saving_bytes),
        )
    ).first()

    return {
        "items": [serializers.media_file(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "aggregate": {
            "count": aggregates[0] if aggregates else 0,
            "total_size": aggregates[1] or 0 if aggregates else 0,
            "potential_saving": aggregates[2] or 0 if aggregates else 0,
        },
    }


@router.get("/files/{file_id}")
def get_file(file_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(MediaFile, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    data = serializers.media_file(row, full=True)
    job_rows = session.execute(
        select(Job).where(Job.file_id == file_id).order_by(Job.created_at.desc()).limit(10)
    ).scalars().all()
    data["jobs"] = [serializers.job(j) for j in job_rows]
    data["exists"] = os.path.exists(row.path)
    return data


class FileAction(BaseModel):
    file_ids: list[int] = Field(default_factory=list)


# States a file may be ignored in, or un-ignored from.  Queued, encoding or
# analysing files belong to a running process, and a converted file has nothing
# left to ignore.
IGNORABLE_STATES = frozenset({
    FileState.NEW.value, FileState.PROBED.value, FileState.CANDIDATE.value,
    FileState.SKIPPED.value, FileState.FAILED.value, FileState.IGNORED.value,
    FileState.MISSING.value,
})
_BUSY_MESSAGE = (
    "Die Datei wird gerade verarbeitet oder ist bereits konvertiert und kann nicht "
    "ignoriert werden. Bitte erst den Job abbrechen."
)


def _unignored_state(row: MediaFile) -> str:
    """Where a file goes back to: a converted file stays done, the rest is
    re-analysed from wherever its metadata allows."""
    if codecs.normalise(row.video_codec) == "av1" and row.converted_at is not None:
        return FileState.DONE.value
    return FileState.PROBED.value if row.video_codec else FileState.NEW.value


def _set_ignored(row: MediaFile, ignored: bool) -> None:
    row.ignored = ignored
    if ignored:
        row.state = FileState.IGNORED.value
    elif row.state == FileState.IGNORED.value:
        row.state = _unignored_state(row)


@router.post("/files/bulk/ignore")
def bulk_ignore(
    payload: FileAction, ignored: bool = True, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """All or nothing: if one of the files is busy, none is changed.

    Declared before ``/files/{file_id}/ignore``, which would otherwise take
    "bulk" for a file id and answer 422.
    """
    rows = [r for r in (session.get(MediaFile, i) for i in payload.file_ids) if r is not None]
    busy = [r for r in rows if r.state not in IGNORABLE_STATES]
    if busy:
        raise HTTPException(
            status_code=409,
            detail=f"{len(busy)} der ausgewaehlten Dateien werden gerade verarbeitet oder "
                   "sind bereits konvertiert. Es wurde nichts geaendert.",
        )
    for row in rows:
        _set_ignored(row, ignored)
    session.commit()
    return {"updated": len(rows)}


@router.post("/files/{file_id}/ignore")
def ignore_file(
    file_id: int, ignored: bool = True, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = session.get(MediaFile, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    if row.state not in IGNORABLE_STATES:
        raise HTTPException(status_code=409, detail=_BUSY_MESSAGE)
    _set_ignored(row, ignored)
    session.commit()
    return serializers.media_file(row)


def _file_row(file_id: int) -> tuple[str, Any]:
    """(path, stored plan) - run in a thread from the async routes."""
    with session_scope() as s:
        row = s.get(MediaFile, file_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Datei nicht gefunden")
        return row.path, row.plan


def _serialized_file(file_id: int) -> dict[str, Any]:
    with session_scope() as s:
        row = s.get(MediaFile, file_id)
        return serializers.media_file(row, full=True) if row else {}


@router.post("/files/{file_id}/analyze")
async def analyze_file(file_id: int, depth: str | None = None) -> dict[str, Any]:
    """Re-run the analysis for one file, on demand, at any depth."""
    settings = load_settings()
    path, _ = await asyncio.to_thread(_file_row, file_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=410, detail="Datei existiert nicht mehr auf der Platte.")

    try:
        info = await ffmpeg.probe(path)
    except ffmpeg.FFmpegError as exc:
        raise HTTPException(status_code=422, detail=f"Datei nicht lesbar: {exc}") from exc

    hw = hwaccel.cached() or await hwaccel.detect(
        settings.hardware.render_device, settings.hardware.qsv_low_power
    )
    advisor = get_advisor(settings.advisor)
    result = await analyzer.analyze(
        info, settings, hw, advisor=advisor, depth=depth, workroot=TRANSCODE_DIR,
    )
    await asyncio.to_thread(scanner._store_probe, file_id, info)
    await asyncio.to_thread(scanner._store_analysis, file_id, result)
    bus.publish("file.analyzed", {"file_id": file_id, "decision": result.decision})
    payload = await asyncio.to_thread(_serialized_file, file_id)
    payload["analysis"] = result.to_dict()
    return payload


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #

class ScanRequest(BaseModel):
    depth: str | None = None
    file_ids: list[int] | None = None


# Scans started from the API.  asyncio keeps only weak references to tasks.
_scan_tasks: set[asyncio.Task] = set()


def _enabled_library_count() -> int:
    with session_scope() as s:
        return s.execute(
            select(func.count(LibraryPath.id)).where(LibraryPath.enabled.is_(True))
        ).scalar() or 0


@router.post("/scan")
async def start_scan(payload: ScanRequest | None = None) -> dict[str, Any]:
    if scanner.state.running:
        raise HTTPException(status_code=409, detail="Es laeuft bereits ein Scan.")
    count = await asyncio.to_thread(_enabled_library_count)
    if not count and not (payload and payload.file_ids):
        raise HTTPException(
            status_code=400,
            detail="Keine Bibliothekspfade konfiguriert. Bitte zuerst unter "
                   "Einstellungen -> Bibliothek einen Ordner hinzufuegen.",
        )
    task = asyncio.create_task(scanner.run_scan(
        trigger="manual",
        depth=payload.depth if payload else None,
        analyze_only_ids=payload.file_ids if payload else None,
    ))
    _scan_tasks.add(task)
    task.add_done_callback(_scan_tasks.discard)
    await asyncio.sleep(0.1)
    return {"ok": True, "status": scanner.state.snapshot()}


@router.post("/scan/cancel")
def cancel_scan() -> dict[str, Any]:
    return {"ok": scanner.cancel_scan()}


@router.get("/scan/status")
def scan_status(session: Session = Depends(get_session)) -> dict[str, Any]:
    latest = session.execute(
        select(ScanRun).order_by(ScanRun.started_at.desc()).limit(1)
    ).scalars().first()
    return {
        "live": scanner.state.snapshot(),
        "last_run": serializers.scan_run(latest) if latest else None,
    }


@router.get("/scan/history")
def scan_history(
    limit: int = Query(20, ge=1, le=100), session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ScanRun).order_by(ScanRun.started_at.desc()).limit(limit)
    ).scalars().all()
    return [serializers.scan_run(r) for r in rows]


class DryRunRequest(BaseModel):
    seconds: int = Field(15, ge=2, le=120, description="Wieviel Material probeweise kodiert wird")
    force_encoder: str | None = Field(
        None, description="Encoder abweichend vom Plan erzwingen, z.B. libsvtav1"
    )
    disable_hw_decode: bool = False


@router.post("/files/{file_id}/dry-run")
async def dry_run(file_id: int, payload: DryRunRequest | None = None) -> dict[str, Any]:
    """Run the planned command against the real file for a few seconds.

    A failing job leaves behind a truncated log and a guess.  This runs the
    exact command the encoder would run - same filters, same streams, same
    parameters - on a short slice, and hands back the complete ffmpeg output.
    It is the difference between "hardware encoding failed" and knowing which
    line failed and why.
    """
    payload = payload or DryRunRequest()
    settings = load_settings()

    path, stored_plan = await asyncio.to_thread(_file_row, file_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=410, detail="Datei existiert nicht mehr auf der Platte.")

    try:
        info = await ffmpeg.probe(path)
    except ffmpeg.FFmpegError as exc:
        raise HTTPException(status_code=422, detail=f"Datei nicht lesbar: {exc}") from exc

    hw = hwaccel.cached() or await hwaccel.detect(
        settings.hardware.render_device, settings.hardware.qsv_low_power
    )
    plan = planner.EncodePlan.from_dict(stored_plan) or planner.build_plan(info, settings, hw)
    if payload.force_encoder:
        plan.encoder = payload.force_encoder
        if payload.force_encoder == "libsvtav1":
            plan.hw_decode = False
            plan.pix_fmt = "yuv420p10le" if plan.pix_fmt.endswith(("10le",)) else "yuv420p"
    if payload.disable_hw_decode:
        plan.hw_decode = False

    dest = TRANSCODE_DIR / f"optimizarr-dryrun-{file_id}.{plan.container}"
    args = planner.build_ffmpeg_args(plan, info, path, str(dest))
    # Insert the duration limit right after the input so only a slice is read.
    limited = list(args)
    try:
        limited.insert(limited.index("-i") + 2, "-t")
        limited.insert(limited.index("-t") + 1, str(payload.seconds))
    except ValueError:
        pass

    started = asyncio.get_running_loop().time()
    try:
        code, err = await ffmpeg.run_with_progress(
            limited, log_lines=500, timeout=max(120, payload.seconds * 20)
        )
    except ffmpeg.FFmpegError as exc:
        code, err = -1, str(exc)
    finally:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
    elapsed = asyncio.get_running_loop().time() - started

    ok = code == 0
    return {
        "ok": ok,
        "returncode": code,
        "seconds": round(elapsed, 1),
        "encoder": plan.encoder,
        "hw_decode": plan.hw_decode,
        "pix_fmt": plan.pix_fmt,
        "command": "ffmpeg " + " ".join(limited),
        "error_line": "" if ok else ffmpeg.first_error_line(err),
        "video_at_fault": None if ok else ffmpeg.failure_is_video(err),
        "output": err,
    }
