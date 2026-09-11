"""Movie overview: every folder that is not a series, Radarr style."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..core import movies, series
from ..db import get_session
from ..models import MediaFile
from .routes_series import COLUMNS, load_groups, tally_dict

router = APIRouter()

# The list shows codec, resolution and verdict of every file without a second request.
_COLUMNS = COLUMNS + (
    MediaFile.width, MediaFile.height, MediaFile.decision_reason, MediaFile.error,
)


def _file(entry: series.Episode) -> dict[str, Any]:
    row = entry.row
    return {
        "id": row.id,
        "path": row.path,
        "name": Path(row.path).name,
        "state": row.state,
        "bucket": entry.bucket,
        "ignored": bool(row.ignored),
        "video_codec": row.video_codec,
        "width": row.width,
        "height": row.height,
        "size": row.size,
        "original_size": row.original_size,
        "estimated_saving_bytes": row.estimated_saving_bytes,
        "decision_reason": row.decision_reason,
        "error": row.error,
    }


@router.get("/movies")
def list_movies(session: Session = Depends(get_session)) -> dict[str, Any]:
    totals = series.Tally()
    items = []
    for entry in load_groups(session, columns=_COLUMNS):
        if entry.looks_like_series:
            continue
        totals.merge(entry.tally)
        title, year = movies.title_year(entry.name)
        # Largest first: the film itself, then other versions and extras.
        files = sorted(entry.episodes, key=lambda e: -(e.row.size or 0))
        items.append({
            "key": entry.key,
            "library_id": entry.library_id,
            "library": entry.library,
            "name": entry.name,
            "title": title,
            "year": year,
            "path": entry.path,
            **tally_dict(entry.tally),
            "files": [_file(e) for e in files],
        })
    items.sort(key=lambda m: (m["title"].casefold(), m["year"] or 0))
    return {"items": items, "totals": tally_dict(totals)}
