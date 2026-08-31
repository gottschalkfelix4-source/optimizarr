"""Library paths, file listing, scanning."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import DEFAULT_MEDIA_ROOT, TRANSCODE_DIR, load_settings
from ..core import analyzer, ffmpeg, hwaccel, scanner
from ..core.advisor import get_advisor
from ..core.events import bus
from ..db import get_session, session_scope
from ..models import FileState, Job, LibraryPath, MediaFile, ScanRun
from . import serializers

log = logging.getLogger(__name__)
router = APIRouter()


# --------------------------------------------------------------------------- #
# Library paths
# --------------------------------------------------------------------------- #

class LibraryPathIn(BaseModel):
    path: str
    name: str = ""
    enabled: bool = True
    profile: str | None = None


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
    path = payload.path.rstrip("/") or "/"
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
    row = LibraryPath(
        path=path, name=payload.name or Path(path).name, enabled=payload.enabled,
        profile=payload.profile,
    )
    session.add(row)
    session.commit()
    bus.publish("library.changed", {"action": "added", "path": path})
    return serializers.library_path(row)


@router.patch("/library/paths/{path_id}")
def update_path(
    path_id: int, payload: dict[str, Any], session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = session.get(LibraryPath, path_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Pfad nicht gefunden")
    for field in ("name", "enabled", "profile"):
        if field in payload:
            setattr(row, field, payload[field])
    session.commit()
    bus.publish("library.changed", {"action": "updated", "path": row.path})
    return serializers.library_path(row)


@router.delete("/library/paths/{path_id}")
def delete_path(
    path_id: int, keep_files: bool = False, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = session.get(LibraryPath, path_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Pfad nicht gefunden")
    path = row.path
    if not keep_files:
        session.query(MediaFile).filter(MediaFile.library_id == path_id).delete()
    session.delete(row)
    session.commit()
    bus.publish("library.changed", {"action": "removed", "path": path})
    return {"ok": True}


@router.get("/library/browse")
def browse(path: str = Query(default="")) -> dict[str, Any]:
    """Directory picker for the settings screen - container-side paths only."""
    target = Path(path or DEFAULT_MEDIA_ROOT)
    if not target.is_absolute():
        target = Path("/") / target
    if not target.is_dir():
        target = Path("/")
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
        conditions.append(MediaFile.video_codec == codec.lower())
    if search:
        like = f"%{search.lower()}%"
        conditions.append(func.lower(MediaFile.path).like(like))
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


@router.post("/files/{file_id}/ignore")
def ignore_file(
    file_id: int, ignored: bool = True, session: Session = Depends(get_session)
) -> dict[str, Any]:
    row = session.get(MediaFile, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    row.ignored = ignored
    if ignored:
        row.state = FileState.IGNORED.value
    elif row.state == FileState.IGNORED.value:
        row.state = FileState.PROBED.value if row.video_codec else FileState.NEW.value
    session.commit()
    return serializers.media_file(row)


@router.post("/files/{file_id}/analyze")
async def analyze_file(file_id: int, depth: str | None = None) -> dict[str, Any]:
    """Re-run the analysis for one file, on demand, at any depth."""
    settings = load_settings()
    with session_scope() as s:
        row = s.get(MediaFile, file_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Datei nicht gefunden")
        path = row.path
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
    with session_scope() as s:
        row = s.get(MediaFile, file_id)
        payload = serializers.media_file(row, full=True) if row else {}
    payload["analysis"] = result.to_dict()
    return payload


@router.post("/files/bulk/ignore")
def bulk_ignore(
    payload: FileAction, ignored: bool = True, session: Session = Depends(get_session)
) -> dict[str, Any]:
    count = 0
    for file_id in payload.file_ids:
        row = session.get(MediaFile, file_id)
        if row is None:
            continue
        row.ignored = ignored
        if ignored:
            row.state = FileState.IGNORED.value
        count += 1
    session.commit()
    return {"updated": count}


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #

class ScanRequest(BaseModel):
    depth: str | None = None
    file_ids: list[int] | None = None


@router.post("/scan")
async def start_scan(payload: ScanRequest | None = None) -> dict[str, Any]:
    if scanner.state.running:
        raise HTTPException(status_code=409, detail="Es laeuft bereits ein Scan.")
    with session_scope() as s:
        count = s.execute(
            select(func.count(LibraryPath.id)).where(LibraryPath.enabled.is_(True))
        ).scalar()
    if not count and not (payload and payload.file_ids):
        raise HTTPException(
            status_code=400,
            detail="Keine Bibliothekspfade konfiguriert. Bitte zuerst unter "
                   "Einstellungen -> Bibliothek einen Ordner hinzufuegen.",
        )
    asyncio.create_task(scanner.run_scan(
        trigger="manual",
        depth=payload.depth if payload else None,
        analyze_only_ids=payload.file_ids if payload else None,
    ))
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
