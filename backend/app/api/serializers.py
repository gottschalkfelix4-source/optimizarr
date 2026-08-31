"""ORM -> JSON helpers shared by the routers."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from ..models import HistoryEntry, Job, LibraryPath, MediaFile, ScanRun


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat()


def media_file(row: MediaFile, full: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": row.id,
        "path": row.path,
        "name": Path(row.path).name,
        "folder": str(Path(row.path).parent),
        "library_id": row.library_id,
        "size": row.size,
        "container": row.container,
        "video_codec": row.video_codec,
        "width": row.width,
        "height": row.height,
        "fps": round(row.fps, 3) if row.fps else 0,
        "duration": row.duration,
        "video_bitrate": row.video_bitrate,
        "bit_depth": row.bit_depth,
        "is_hdr": row.is_hdr,
        "hdr_format": row.hdr_format,
        "interlaced": row.interlaced,
        "state": row.state,
        "ignored": row.ignored,
        "error": row.error,
        "estimated_size": row.estimated_size,
        "estimated_saving_bytes": row.estimated_saving_bytes,
        "estimated_saving_pct": round(row.estimated_saving_pct, 1),
        "confidence": round(row.confidence, 3),
        "decision_reason": row.decision_reason,
        "advisor_note": row.advisor_note,
        "analysis_depth": row.analysis_depth,
        "analyzed_at": iso(row.analyzed_at),
        "original_size": row.original_size,
        "converted_at": iso(row.converted_at),
        "measured_vmaf": row.measured_vmaf,
        "audio_count": len(row.audio_streams or []),
        "subtitle_count": len(row.subtitle_streams or []),
    }
    if full:
        data["audio_streams"] = row.audio_streams or []
        data["subtitle_streams"] = row.subtitle_streams or []
        data["plan"] = row.plan
        data["profile"] = row.profile
        data["pix_fmt"] = row.pix_fmt
        data["color_transfer"] = row.color_transfer
        data["first_seen"] = iso(row.first_seen)
        data["last_seen"] = iso(row.last_seen)
    return data


def job(row: Job, include_log: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": row.id,
        "file_id": row.file_id,
        "state": row.state,
        "priority": row.priority,
        "progress": round(row.progress, 4),
        "speed": round(row.speed, 2),
        "fps": round(row.fps, 1),
        "eta_seconds": row.eta_seconds,
        "current_size": row.current_size,
        "input_size": row.input_size,
        "output_size": row.output_size,
        "predicted_size": row.predicted_size,
        "vmaf": row.vmaf,
        "error": row.error,
        "created_at": iso(row.created_at),
        "started_at": iso(row.started_at),
        "finished_at": iso(row.finished_at),
        "plan": row.plan,
    }
    if row.file is not None:
        data["path"] = row.file.path
        data["name"] = Path(row.file.path).name
        data["duration"] = row.file.duration
        data["resolution"] = f"{row.file.width}x{row.file.height}" if row.file.width else ""
    if include_log:
        data["log"] = row.log
    return data


def library_path(row: LibraryPath, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "id": row.id,
        "path": row.path,
        "name": row.name or Path(row.path).name or row.path,
        "enabled": row.enabled,
        "profile": row.profile,
        "created_at": iso(row.created_at),
    }
    if stats:
        data.update(stats)
    return data


def history(row: HistoryEntry) -> dict[str, Any]:
    return {
        "id": row.id,
        "level": row.level,
        "category": row.category,
        "message": row.message,
        "file_id": row.file_id,
        "detail": row.detail,
        "created_at": iso(row.created_at),
    }


def scan_run(row: ScanRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "state": row.state,
        "trigger": row.trigger,
        "files_seen": row.files_seen,
        "files_new": row.files_new,
        "files_probed": row.files_probed,
        "files_analyzed": row.files_analyzed,
        "candidates": row.candidates,
        "total": row.total,
        "current_path": row.current_path,
        "error": row.error,
        "started_at": iso(row.started_at),
        "finished_at": iso(row.finished_at),
    }
