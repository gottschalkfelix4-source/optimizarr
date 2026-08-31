"""Queue, jobs, statistics, history and the live event stream."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from ..config import load_settings, update_settings
from ..core import predictor, scanner, worker
from ..core.events import bus
from ..db import get_session, session_scope
from ..models import (
    FileState, HistoryEntry, Job, JobState, LearningSample, MediaFile, utcnow,
)
from . import serializers

log = logging.getLogger(__name__)
router = APIRouter()


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #

class EnqueueRequest(BaseModel):
    file_ids: list[int] = Field(default_factory=list)
    priority: int | None = None
    all_candidates: bool = False
    min_saving_pct: float | None = None
    limit: int | None = None


@router.post("/jobs")
def enqueue(payload: EnqueueRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    file_ids = list(payload.file_ids)
    if payload.all_candidates:
        query = select(MediaFile.id).where(
            MediaFile.state == FileState.CANDIDATE.value,
            MediaFile.ignored.is_(False),
        )
        if payload.min_saving_pct is not None:
            query = query.where(MediaFile.estimated_saving_pct >= payload.min_saving_pct)
        query = query.order_by(MediaFile.estimated_saving_bytes.desc())
        if payload.limit:
            query = query.limit(payload.limit)
        file_ids = list(session.execute(query).scalars().all())

    if not file_ids:
        return {"added": 0, "skipped": [], "message": "Keine passenden Dateien gefunden."}
    added, skipped = worker.enqueue_files(file_ids, payload.priority)
    return {
        "added": added,
        "skipped": skipped[:20],
        "message": f"{added} Datei(en) eingereiht."
                   + (f" {len(skipped)} uebersprungen." if skipped else ""),
    }


@router.get("/jobs")
def list_jobs(
    session: Session = Depends(get_session),
    state: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    query = (
        select(Job)
        .options(selectinload(Job.file))
        .order_by(
            case((Job.state == JobState.RUNNING.value, 0),
                 (Job.state == JobState.QUEUED.value, 1), else_=2),
            Job.priority.asc(),
            Job.created_at.desc(),
        )
        .limit(limit)
    )
    if state and state != "all":
        if state == "active":
            query = query.where(Job.state.in_([JobState.QUEUED.value, JobState.RUNNING.value]))
        elif state == "finished":
            query = query.where(Job.state.in_([
                JobState.DONE.value, JobState.FAILED.value,
                JobState.REJECTED.value, JobState.CANCELLED.value,
            ]))
        else:
            query = query.where(Job.state == state)
    rows = session.execute(query).scalars().all()

    counts = dict(session.execute(
        select(Job.state, func.count(Job.id)).group_by(Job.state)
    ).all())
    return {
        "items": [serializers.job(r) for r in rows],
        "counts": counts,
        "worker": worker.queue_worker.status(),
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.execute(
        select(Job).options(selectinload(Job.file)).where(Job.id == job_id)
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    return serializers.job(row, include_log=True)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(Job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    if row.state == JobState.RUNNING.value:
        if worker.queue_worker.cancel_job(job_id):
            return {"ok": True, "message": "Abbruch angefordert."}
        # Running in the DB but not in the worker: a restart lost it.
        row.state = JobState.CANCELLED.value
        row.finished_at = utcnow()
        session.commit()
        return {"ok": True, "message": "Verwaister Job aufgeraeumt."}
    if row.state == JobState.QUEUED.value:
        row.state = JobState.CANCELLED.value
        row.finished_at = utcnow()
        media = session.get(MediaFile, row.file_id)
        if media and media.state == FileState.QUEUED.value:
            media.state = FileState.CANDIDATE.value
        session.commit()
        bus.publish("queue.changed", {})
        return {"ok": True, "message": "Aus der Warteschlange entfernt."}
    raise HTTPException(status_code=409, detail=f"Job ist bereits {row.state}.")


@router.delete("/jobs/finished")
def clear_finished(session: Session = Depends(get_session)) -> dict[str, Any]:
    removed = session.query(Job).filter(Job.state.in_([
        JobState.DONE.value, JobState.FAILED.value,
        JobState.REJECTED.value, JobState.CANCELLED.value,
    ])).delete(synchronize_session=False)
    session.commit()
    return {"removed": removed}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(Job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    if row.state in (JobState.QUEUED.value, JobState.RUNNING.value):
        raise HTTPException(status_code=409, detail="Job laeuft bereits.")
    added, skipped = worker.enqueue_files([row.file_id])
    if not added:
        raise HTTPException(status_code=409, detail=skipped[0] if skipped else "Nicht moeglich.")
    return {"ok": True, "message": "Erneut eingereiht."}


class QueueControl(BaseModel):
    paused: bool


@router.post("/queue/pause")
def pause_queue(payload: QueueControl) -> dict[str, Any]:
    update_settings({"queue": {"paused": payload.paused}})
    bus.publish("queue.changed", {"paused": payload.paused})
    return {"paused": payload.paused, "worker": worker.queue_worker.status()}


@router.post("/queue/reorder")
def reorder(payload: dict[str, Any], session: Session = Depends(get_session)) -> dict[str, Any]:
    """Accepts {"order": [job_id, ...]} - index becomes the priority."""
    order = payload.get("order") or []
    for index, job_id in enumerate(order):
        row = session.get(Job, int(job_id))
        if row and row.state == JobState.QUEUED.value:
            row.priority = index
    session.commit()
    bus.publish("queue.changed", {})
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #

@router.get("/stats")
def stats(session: Session = Depends(get_session)) -> dict[str, Any]:
    by_state = dict(session.execute(
        select(MediaFile.state, func.count(MediaFile.id)).group_by(MediaFile.state)
    ).all())

    totals = session.execute(
        select(func.count(MediaFile.id), func.sum(MediaFile.size), func.sum(MediaFile.duration))
    ).first()

    potential = session.execute(
        select(func.sum(MediaFile.estimated_saving_bytes), func.count(MediaFile.id))
        .where(MediaFile.state == FileState.CANDIDATE.value, MediaFile.ignored.is_(False))
    ).first()

    realised = session.execute(
        select(
            func.sum(Job.input_size - Job.output_size),
            func.count(Job.id),
            func.avg(Job.vmaf),
        ).where(Job.state == JobState.DONE.value)
    ).first()

    codecs = session.execute(
        select(MediaFile.video_codec, func.count(MediaFile.id), func.sum(MediaFile.size))
        .where(MediaFile.video_codec != "")
        .group_by(MediaFile.video_codec)
        .order_by(func.sum(MediaFile.size).desc())
        .limit(12)
    ).all()

    resolutions = session.execute(
        select(MediaFile.height, func.count(MediaFile.id), func.sum(MediaFile.size))
        .where(MediaFile.height > 0)
        .group_by(MediaFile.height)
    ).all()

    # Saved bytes per day for the sparkline.
    daily = session.execute(
        select(
            func.date(Job.finished_at),
            func.sum(Job.input_size - Job.output_size),
            func.count(Job.id),
        )
        .where(Job.state == JobState.DONE.value, Job.finished_at.isnot(None))
        .group_by(func.date(Job.finished_at))
        .order_by(func.date(Job.finished_at).desc())
        .limit(60)
    ).all()

    top = session.execute(
        select(MediaFile)
        .where(MediaFile.state == FileState.CANDIDATE.value, MediaFile.ignored.is_(False))
        .order_by(MediaFile.estimated_saving_bytes.desc())
        .limit(8)
    ).scalars().all()

    return {
        "files": {
            "total": totals[0] or 0,
            "total_size": totals[1] or 0,
            "total_duration": totals[2] or 0,
            "by_state": by_state,
        },
        "potential": {
            "saving_bytes": potential[0] or 0,
            "candidate_count": potential[1] or 0,
        },
        "realised": {
            "saved_bytes": realised[0] or 0,
            "converted_count": realised[1] or 0,
            "average_vmaf": round(realised[2], 1) if realised[2] else None,
        },
        "codecs": [
            {"codec": c or "unbekannt", "count": n, "size": s or 0} for c, n, s in codecs
        ],
        "resolutions": _bucket_resolutions(resolutions),
        "daily": [
            {"date": str(d), "saved": int(saved or 0), "count": n}
            for d, saved, n in reversed(daily)
        ],
        "top_candidates": [serializers.media_file(r) for r in top],
        "model": predictor.model().stats(),
    }


def _bucket_resolutions(rows: list[Any]) -> list[dict[str, Any]]:
    buckets = [
        (0, 576, "SD"), (577, 800, "720p"), (801, 1200, "1080p"),
        (1201, 1600, "1440p"), (1601, 2400, "4K"), (2401, 10000, "8K+"),
    ]
    out: dict[str, dict[str, Any]] = {
        label: {"label": label, "count": 0, "size": 0} for _, _, label in buckets
    }
    for height, count, size in rows:
        for low, high, label in buckets:
            if low <= (height or 0) <= high:
                out[label]["count"] += count
                out[label]["size"] += size or 0
                break
    return [v for v in out.values() if v["count"]]


@router.get("/stats/model")
def model_stats(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Prediction accuracy over time - the 'is the AI learning?' view."""
    rows = session.execute(
        select(LearningSample).order_by(LearningSample.created_at.desc()).limit(200)
    ).scalars().all()
    points = []
    for r in reversed(rows):
        if r.predicted_bitrate <= 0 or r.actual_bitrate <= 0:
            continue
        error = (r.actual_bitrate - r.predicted_bitrate) / r.predicted_bitrate * 100
        points.append({
            "created_at": serializers.iso(r.created_at),
            "predicted_kbps": round(r.predicted_bitrate / 1000),
            "actual_kbps": round(r.actual_bitrate / 1000),
            "error_pct": round(error, 1),
            "encoder": r.encoder,
            "crf": r.crf,
            "source_codec": r.source_codec,
            "vmaf": r.actual_vmaf,
        })
    return {"stats": predictor.model().stats(), "samples": points}


@router.get("/history")
def history(
    session: Session = Depends(get_session),
    limit: int = Query(60, ge=1, le=300),
    level: str | None = None,
) -> list[dict[str, Any]]:
    query = select(HistoryEntry).order_by(HistoryEntry.created_at.desc()).limit(limit)
    if level and level != "all":
        query = query.where(HistoryEntry.level == level)
    rows = session.execute(query).scalars().all()
    return [serializers.history(r) for r in rows]


# --------------------------------------------------------------------------- #
# Live updates
# --------------------------------------------------------------------------- #

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = bus.subscribe()
    try:
        await websocket.send_text(json.dumps({
            "type": "hello",
            "data": {
                "scan": scanner.state.snapshot(),
                "queue": worker.queue_worker.status(),
                "recent": bus.recent()[-10:],
            },
        }, default=str))
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25)
                await websocket.send_text(json.dumps(event, default=str))
            except asyncio.TimeoutError:
                # Keep proxies (and Unraid's reverse proxy) from closing the socket.
                await websocket.send_text(json.dumps({"type": "ping"}))
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        pass
    except Exception:  # pragma: no cover
        log.debug("websocket closed unexpectedly", exc_info=True)
    finally:
        bus.unsubscribe(queue)
