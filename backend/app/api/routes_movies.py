"""Movie overview: every folder that is not a series, Radarr style."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..core import movies, series, group_cache, worker
from ..db import get_session
from .routes_series import COLUMNS, load_groups, tally_dict

router = APIRouter()

# The list shows codec, resolution and verdict of every file without a second request.
_COLUMNS = COLUMNS


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


def _build_overview(session: Session) -> dict[str, Any]:
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


def _overview(session: Session) -> dict[str, Any]:
    return group_cache.snapshot(session, lambda: _build_overview(session), key="movies")


@router.get("/movies")
def list_movies(
    session: Session = Depends(get_session), search: str = Query("", max_length=500), filter: group_cache.GroupFilter = "all",
    sort: group_cache.GroupSort = "name", page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    overview = _overview(session)
    result = group_cache.browse(overview["items"], search, filter, sort, page, page_size)
    filtered = result.pop("filtered")
    files = [f for item in filtered for f in item["files"]]
    result["pending_count"] = sum(f["bucket"] == "pending" for f in files)
    result["force_count"] = sum(f["bucket"] in {"pending", "excluded", "failed", "other"} and f["state"] != "missing" for f in files)
    return {**result, "totals": overview["totals"]}


@router.post("/movies/enqueue")
def enqueue_movies(
    search: str = Query("", max_length=500), filter: group_cache.GroupFilter = "all", force: bool = False,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    overview = _overview(session)
    filtered = group_cache.browse(overview["items"], search, filter, "name", 1, 50)["filtered"]
    ids = [f["id"] for item in filtered for f in item["files"] if (
        f["bucket"] in {"pending", "excluded", "failed", "other"} and f["state"] != "missing"
        if force else f["bucket"] == "pending"
    )]
    added, skipped = worker.enqueue_files(ids, force=force)
    return {"added": added, "skipped": skipped[:20], "message": f"{added} Datei(en) eingereiht."}
