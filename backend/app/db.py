"""Database engine + session management.

Single-file SQLite lives on the appdata volume so it survives container
recreation.  WAL mode keeps the UI responsive while the encoder worker writes.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

CONFIG_DIR = Path(os.environ.get("OPTIMIZARR_CONFIG_DIR", "/config"))
DB_PATH = CONFIG_DIR / "optimizarr.db"

_engine = None
_SessionLocal: sessionmaker | None = None


def _build_engine():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # pragma: no cover - driver level
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return engine


def engine():
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope.  Commits on success, rolls back on error."""
    s = session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    s = session_factory()()
    try:
        yield s
    finally:
        s.close()
