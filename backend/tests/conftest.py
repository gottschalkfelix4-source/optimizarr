"""Shared test setup.

* The config and transcode directories default to a fresh temporary directory
  per test run, so no test ever reads or writes a database left behind by an
  earlier run (or by a parallel one).  This runs before any test module
  imports ``app``, which is when those paths are fixed.
* Every ``TestClient`` sends the CSRF header the API requires for writes, the
  way the web UI does.  Tests of the CSRF check itself remove it explicitly.
* ``hermetic_client`` runs the real app with its lifespan against an
  in-memory database, without hardware detection or a startup scan.
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

_RUN_DIR = Path(tempfile.mkdtemp(prefix="optimizarr-tests-"))
os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(_RUN_DIR / "config"))
os.environ.setdefault("OPTIMIZARR_TRANSCODE_DIR", str(_RUN_DIR / "transcode"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

CSRF_HEADERS = {"X-Optimizarr": "1"}

_original_init = TestClient.__init__


def _init_with_csrf_header(self, *args, headers=None, **kwargs):
    merged = dict(CSRF_HEADERS)
    merged.update(headers or {})
    _original_init(self, *args, headers=merged, **kwargs)


TestClient.__init__ = _init_with_csrf_header  # type: ignore[method-assign]


@contextmanager
def isolated_app_state(tmp: Path) -> Iterator[None]:
    """In-memory database, private directories, quiet startup settings."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app import config, db, main
    from app.models import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(db, "_engine", engine)
        mp.setattr(db, "_SessionLocal", None)
        mp.setattr(config, "_cache", None)
        for module in (config, main):
            mp.setattr(module, "CONFIG_DIR", tmp / "config")
            mp.setattr(module, "TRANSCODE_DIR", tmp / "transcode")
        settings = config.AppSettings()
        settings.hardware.detect_on_start = False
        settings.library.scan_on_start = False
        settings.library.scan_interval_hours = 0
        config.save_settings(settings)
        yield
    finally:
        mp.undo()
        engine.dispose()


@pytest.fixture(scope="module")
def hermetic_client(tmp_path_factory) -> Iterator[TestClient]:
    from app.main import app

    with isolated_app_state(tmp_path_factory.mktemp("app")):
        with TestClient(app, headers=CSRF_HEADERS) as client:
            yield client
