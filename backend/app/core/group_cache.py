"""Bounded, transaction-invalidated snapshots for movie/series browsing."""
from __future__ import annotations
from typing import Literal
import threading
import time
from weakref import WeakKeyDictionary
from sqlalchemy import event
from sqlalchemy.orm import Session
from ..models import MediaFile, LibraryPath

GroupFilter = Literal["all", "open", "complete", "candidates", "excluded", "failed", "active"]
GroupSort = Literal["name", "title", "year", "progress", "size", "saved", "potential"]

_lock = threading.RLock()
_cache = WeakKeyDictionary()
_versions = WeakKeyDictionary()


def invalidate(session: Session) -> None:
    bind = session.get_bind()
    with _lock:
        _versions[bind] = _versions.get(bind, 0) + 1
        _cache.pop(bind, None)


@event.listens_for(Session, "before_flush")
def _track(session, *_):
    if any(isinstance(row, (MediaFile, LibraryPath)) for row in (*session.new, *session.dirty, *session.deleted)):
        session.info["groups_changed"] = True


@event.listens_for(Session, "do_orm_execute")
def _track_bulk(state):
    if (state.is_update or state.is_delete) and getattr(getattr(state.statement, "table", None), "name", None) in ("media_files", "library_paths"):
        state.session.info["groups_changed"] = True


@event.listens_for(Session, "after_commit")
def _committed(session):
    if session.info.pop("groups_changed", False):
        invalidate(session)


@event.listens_for(Session, "after_rollback")
def _rolled_back(session):
    session.info.pop("groups_changed", None)


def snapshot(session: Session, build, key="groups"):
    # Never cache a snapshot containing this transaction's uncommitted edits.
    if session.new or session.dirty or session.deleted or session.info.get("groups_changed"):
        return build()
    bind = session.get_bind()
    with _lock:
        cached = _cache.get(bind, {}).get(key)
        version = _versions.get(bind, 0)
        if cached and cached[0] == version and time.monotonic() - cached[1] < 30:
            return cached[2]
    # Building can query/sort 100k rows. Writers must still be able to invalidate.
    result = build()
    with _lock:
        if _versions.get(bind, 0) == version:
            _cache.setdefault(bind, {})[key] = (version, time.monotonic(), result)
    return result


def browse(items: list[dict], search: str, state: str, sort: str, page: int, page_size: int) -> dict:
    needle = search.strip().casefold()
    filtered = []
    for item in items:
        if needle and needle not in f'{item.get("title", item["name"])} {item.get("year") or ""} {item["name"]}'.casefold():
            continue
        complete = item["episodes"] > 0 and item["in_av1"] == item["episodes"]
        if state == "open" and complete or state == "complete" and not complete:
            continue
        bucket = {"candidates": "pending", "excluded": "excluded", "failed": "failed", "active": "active"}.get(state)
        if bucket and not item["counts"][bucket]:
            continue
        filtered.append(item)
    def order(item):
        title = item.get("title", item["name"]).casefold()
        field = {"size": "total_size", "saved": "saved_bytes", "potential": "potential_saving", "year": "year"}.get(sort)
        if field:
            return (-(item.get(field) or 0), title, item["key"])
        if sort == "progress":
            return (item["in_av1"] / max(1, item["episodes"]), title, item["key"])
        return (title, item["key"])
    filtered.sort(key=order)
    pages = max(1, (len(filtered) + page_size - 1) // page_size)
    page = min(page, pages)
    return {
        "items": filtered[(page-1)*page_size:page*page_size],
        "total": len(filtered), "all_count": len(items), "page": page,
        "page_size": page_size, "pages": pages,
        "complete_count": sum(i["episodes"] > 0 and i["in_av1"] == i["episodes"] for i in items),
        "filtered": filtered,
    }
