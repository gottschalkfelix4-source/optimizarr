"""Series overview: the library grouped by series and season, Sonarr style."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import series, worker
from ..db import get_session
from ..models import LibraryPath, MediaFile
from . import serializers

router = APIRouter()

# Only what the grouping needs - stream lists and plans stay in the database.
COLUMNS = (
    MediaFile.id, MediaFile.path, MediaFile.library_id, MediaFile.state,
    MediaFile.video_codec, MediaFile.ignored, MediaFile.size, MediaFile.original_size,
    MediaFile.estimated_saving_bytes, MediaFile.converted_at,
)


def load_groups(
    session: Session, library_id: int | None = None, columns: tuple[Any, ...] = COLUMNS,
) -> list[series.SeriesGroup]:
    """Every folder group, series and movies alike."""
    libraries = {
        row.id: (row.path, row.name or Path(row.path).name or row.path)
        for row in session.execute(select(LibraryPath)).scalars()
    }
    query = select(*columns).where(MediaFile.library_id.is_not(None))
    if library_id is not None:
        query = query.where(MediaFile.library_id == library_id)
    return series.group(session.execute(query).all(), libraries)


def _series(session: Session, library_id: int | None = None) -> list[series.SeriesGroup]:
    return [g for g in load_groups(session, library_id) if g.looks_like_series]


def _find(session: Session, key: str) -> series.SeriesGroup:
    parsed = series.split_key(key)
    if parsed is not None:
        for entry in _series(session, parsed[0]):
            if entry.key == key:
                return entry
    raise HTTPException(status_code=404, detail="Serie nicht gefunden")


def tally_dict(tally: series.Tally) -> dict[str, Any]:
    return {**tally.as_dict(), "last_converted": serializers.iso(tally.last_converted)}


def _summary(entry: series.SeriesGroup) -> dict[str, Any]:
    return {
        "key": entry.key,
        "library_id": entry.library_id,
        "library": entry.library,
        "name": entry.name,
        "path": entry.path,
        "season_count": sum(1 for s in entry.seasons if s),
        **tally_dict(entry.tally),
    }


@router.get("/series")
def list_series(session: Session = Depends(get_session)) -> dict[str, Any]:
    entries = _series(session)
    totals = series.Tally()
    for entry in entries:
        totals.merge(entry.tally)
    return {"items": [_summary(e) for e in entries], "totals": tally_dict(totals)}


@router.get("/series/detail")
def series_detail(key: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    entry = _find(session, key)
    rows = {
        row.id: row
        for row in session.execute(
            select(MediaFile).where(MediaFile.id.in_([e.file_id for e in entry.episodes]))
        ).scalars()
    }
    seasons = []
    for number in sorted(entry.seasons, key=series.season_order):
        episodes = sorted(
            (e for e in entry.episodes if e.season == number and e.file_id in rows),
            key=lambda e: (e.episode is None, e.episode or 0, rows[e.file_id].path.casefold()),
        )
        seasons.append({
            "season": number,
            "label": series.season_label(number),
            **tally_dict(entry.seasons[number]),
            # Not "episodes": that is the count from the tally.
            "files": [
                {
                    **serializers.media_file(rows[e.file_id]),
                    "season": e.season, "episode": e.episode, "bucket": e.bucket,
                }
                for e in episodes
            ],
        })
    return {**_summary(entry), "seasons": seasons}


class SeriesEnqueue(BaseModel):
    key: str
    season: int | None = None       # None: every season
    force: bool = False


@router.post("/series/enqueue")
def enqueue_series(
    payload: SeriesEnqueue, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Queue a whole series or one season.

    Without ``force`` only candidates go in, as "Kandidaten einreihen" does
    elsewhere.  With it, everything not yet converted, on its way or already AV1
    goes in - codec exclusions, ignore flags and skip verdicts notwithstanding.
    """
    entry = _find(session, payload.key)
    wanted = series.FORCEABLE if payload.force else ("pending",)
    file_ids = [
        e.file_id for e in entry.episodes
        if e.bucket in wanted and (payload.season is None or e.season == payload.season)
    ]
    if not file_ids:
        return {"added": 0, "skipped": [], "message": "Nichts einzureihen."}
    added, skipped = worker.enqueue_files(file_ids, force=payload.force)
    return {
        "added": added,
        "skipped": skipped[:20],
        "message": f"{added} Folge(n) eingereiht."
                   + (f" {len(skipped)} uebersprungen." if skipped else ""),
    }
