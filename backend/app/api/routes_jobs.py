"""Queue, jobs, statistics, history and the live event stream."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from ..config import load_settings, update_settings
from ..core import planner, predictor, scanner, worker
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
    file_ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(default_factory=list, max_length=10000)
    priority: int | None = Field(None, ge=0, le=1000000)
    all_candidates: bool = False
    min_saving_pct: float | None = Field(None, ge=0, le=100)
    limit: int | None = Field(None, ge=1, le=10000)
    # Queue despite exclusions and skip verdicts - see worker.enqueue_files.
    force: bool = False


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
    added, skipped = worker.enqueue_files(file_ids, payload.priority, force=payload.force)
    return {
        "added": added,
        "skipped": skipped[:20],
        "message": f"{added} Datei(en) eingereiht."
                   + (f" {len(skipped)} uebersprungen." if skipped else ""),
    }


FINISHED_JOB_STATES = (
    JobState.DONE.value, JobState.FAILED.value, JobState.REJECTED.value, JobState.CANCELLED.value,
)
JOB_STATE_FILTERS = frozenset({"all", "active", "finished", *(s.value for s in JobState)})


@router.get("/jobs")
def list_jobs(
    session: Session = Depends(get_session),
    state: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    """Jobs in queue order.  ``state`` is ``active`` (queued + running),
    ``finished``, ``all`` or a single job state; ``counts`` always covers
    every job, whatever the filter and limit."""
    if state and state not in JOB_STATE_FILTERS:
        raise HTTPException(
            status_code=422,
            detail=f"Unbekannter Filter '{state}'. Erlaubt: {', '.join(sorted(JOB_STATE_FILTERS))}.",
        )
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
            query = query.where(Job.state.in_(FINISHED_JOB_STATES))
        else:
            query = query.where(Job.state == state)
    rows = session.execute(query).scalars().all()

    counts = {s.value: 0 for s in JobState}
    counts.update(dict(session.execute(
        select(Job.state, func.count(Job.id)).group_by(Job.state)
    ).all()))
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
    from ..core.output_files import pending_commits
    if job_id in pending_commits()["jobs"]:
        raise HTTPException(409, "Die Dateiuebernahme wird noch verbucht; bitte Wiederherstellung abwarten.")
    row = session.get(Job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    if row.state == JobState.RUNNING.value:
        if worker.queue_worker.cancel_job(job_id):
            return {"ok": True, "message": "Abbruch angefordert."}
        # Running in the DB but not in the worker: a restart lost it.
        row.state = JobState.CANCELLED.value
        row.finished_at = utcnow()
        media = session.get(MediaFile, row.file_id)
        if media and media.state == FileState.ENCODING.value:
            media.state = (
                (row.plan or {}).get(planner.RESTORE_STATE) or FileState.CANDIDATE.value
            )
        session.commit()
        bus.publish("queue.changed", {})
        return {"ok": True, "message": "Verwaister Job aufgeraeumt."}
    if row.state == JobState.QUEUED.value:
        row.state = JobState.CANCELLED.value
        row.finished_at = utcnow()
        media = session.get(MediaFile, row.file_id)
        if media and media.state == FileState.QUEUED.value:
            # A forced job goes back to wherever the file was - calling an
            # excluded file a candidate would have auto-queue pick it up.
            media.state = (
                (row.plan or {}).get(planner.RESTORE_STATE) or FileState.CANDIDATE.value
            )
        session.commit()
        bus.publish("queue.changed", {})
        return {"ok": True, "message": "Aus der Warteschlange entfernt."}
    raise HTTPException(status_code=409, detail=f"Job ist bereits {row.state}.")


@router.delete("/jobs/finished")
def clear_finished(session: Session = Depends(get_session)) -> dict[str, Any]:
    removed = session.query(Job).filter(Job.state.in_(FINISHED_JOB_STATES)).delete(
        synchronize_session=False
    )
    session.commit()
    return {"removed": removed}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(Job, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    if row.state in (JobState.QUEUED.value, JobState.RUNNING.value):
        raise HTTPException(status_code=409, detail="Job laeuft bereits.")
    added, skipped = worker.enqueue_files([row.file_id], force=planner.is_forced(row.plan))
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


class StartNow(BaseModel):
    active: bool = True


@router.post("/queue/start-now")
def start_now(payload: StartNow) -> dict[str, Any]:
    """Start the queue outside the schedule - until it has run dry."""
    if payload.active:
        if not worker._has_queued_jobs():
            raise HTTPException(status_code=409, detail="Keine wartenden Jobs.")
        worker.queue_worker.start_now()
    else:
        worker.queue_worker.end_override()
    bus.publish("queue.changed", {})
    return {"active": worker.queue_worker.schedule_override, "worker": worker.queue_worker.status()}


class ReorderRequest(BaseModel):
    order: list[Annotated[int, Field(strict=True, gt=0)]] = Field(default_factory=list, max_length=10000)


@router.post("/queue/reorder")
def reorder(payload: ReorderRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Accepts {"order": [job_id, ...]} - index becomes the priority."""
    order = list(dict.fromkeys(payload.order))
    for index, job_id in enumerate(order):
        row = session.get(Job, job_id)
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

    # Counted from the files, not from the jobs: "clear finished" deletes the
    # jobs and took the whole saving with it.  original_size is only set when
    # the new file replaced the old one, so sidecar output claims nothing.
    replaced = (MediaFile.state == FileState.DONE.value) & (MediaFile.original_size > 0)
    realised = session.execute(
        select(
            func.sum(MediaFile.original_size - MediaFile.size),
            func.count(MediaFile.id),
            func.avg(case((MediaFile.quality_metric == "vmaf", MediaFile.quality_value), else_=None)),
        ).where(replaced)
    ).first()

    codecs = session.execute(
        select(MediaFile.video_codec, func.count(MediaFile.id), func.sum(MediaFile.size))
        .where(MediaFile.video_codec != "")
        .group_by(MediaFile.video_codec)
        .order_by(func.sum(MediaFile.size).desc())
        .limit(12)
    ).all()

    resolutions = session.execute(
        select(
            MediaFile.width, MediaFile.height, func.count(MediaFile.id), func.sum(MediaFile.size)
        )
        .where((MediaFile.height > 0) | (MediaFile.width > 0))
        .group_by(MediaFile.width, MediaFile.height)
    ).all()

    # Saved bytes per day for the sparkline.
    daily = session.execute(
        select(
            func.date(MediaFile.converted_at),
            func.sum(MediaFile.original_size - MediaFile.size),
            func.count(MediaFile.id),
        )
        .where(replaced, MediaFile.converted_at.isnot(None))
        .group_by(func.date(MediaFile.converted_at))
        .order_by(func.date(MediaFile.converted_at).desc())
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


RESOLUTION_CLASSES = ("SD", "720p", "1080p", "1440p", "2160p")


def resolution_class(width: int | None, height: int | None) -> str:
    """By width first, height as fallback - a 1920x800 scope film is 1080p,
    not 720p.  Must match ``resolutionClass`` in the frontend's format.ts."""
    w, h = width or 0, height or 0
    if w >= 3200 or h >= 1800:
        return "2160p"
    if w >= 2200 or h >= 1260:
        return "1440p"
    if w >= 1700 or h >= 900:
        return "1080p"
    if w >= 1100 or h >= 620:
        return "720p"
    return "SD"


def _bucket_resolutions(rows: list[Any]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        label: {"label": label, "count": 0, "size": 0} for label in RESOLUTION_CLASSES
    }
    for width, height, count, size in rows:
        label = resolution_class(width, height)
        out[label]["count"] += count
        out[label]["size"] += size or 0
    return [v for v in out.values() if v["count"]]


@router.get("/stats/model")
def model_stats(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Prediction accuracy over time - the 'is the AI learning?' view."""
    rows = session.execute(
        select(LearningSample).order_by(LearningSample.created_at.desc()).limit(200)
    ).scalars().all()
    points = []
    evaluated = []
    for r in reversed(rows):
        if r.predicted_bitrate <= 0 or r.actual_bitrate <= 0:
            continue
        prediction = r.applied_bitrate or r.predicted_bitrate
        error = (r.actual_bitrate - prediction) / prediction * 100
        if r.applied_bitrate and r.applied_bitrate > 0:
            evaluated.append((r.encoder, abs(r.actual_bitrate - r.applied_bitrate) / r.actual_bitrate * 100))
        points.append({
            "created_at": serializers.iso(r.created_at),
            "predicted_kbps": round(prediction / 1000),
            "prediction_kind": "applied" if r.applied_bitrate else "legacy_base",
            "actual_kbps": round(r.actual_bitrate / 1000),
            "error_pct": round(error, 1),
            "encoder": r.encoder,
            "crf": r.crf,
            "source_codec": r.source_codec,
            "vmaf": r.actual_vmaf,
            "quality_metric": r.quality_metric,
            "quality_value": r.quality_value,
        })
    encoders = sorted({encoder for encoder, _ in evaluated})
    evaluation = {
        "samples": len(evaluated),
        "mean_abs_error_pct": round(sum(error for _, error in evaluated) / len(evaluated), 2) if evaluated else None,
        "encoders": [{"encoder": encoder, "samples": sum(enc == encoder for enc, _ in evaluated),
                      "mean_abs_error_pct": round(sum(err for enc, err in evaluated if enc == encoder) / sum(enc == encoder for enc, _ in evaluated), 2)} for encoder in encoders],
    }
    return {"stats": predictor.model().stats(), "evaluation": evaluation, "samples": points}


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
