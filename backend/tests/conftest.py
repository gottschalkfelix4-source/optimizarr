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
# Desktop-specific process settings must not affect protocol assertions.
for override in ("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_APP_SERVER_LOGIN_CLIENT_ID", "CODEX_ISSUER_OVERRIDE", "CODEX_REFRESH_TOKEN_URL_OVERRIDE"):
    os.environ.pop(override, None)
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
    from app.core import scanner
    from app.models import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    mp = pytest.MonkeyPatch()
    # A scan the app starts in the background (e.g. the re-analysis a codec
    # exclusion change requests) dies with the TestClient's event loop and
    # would otherwise leave "a scan is running" behind for the next module.
    mp.setattr(scanner, "state", scanner.ScanState())
    mp.setattr(scanner, "_pending_analysis", set())
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


@pytest.fixture(autouse=True)
def isolated_output_modules(tmp_path, tmp_path_factory, monkeypatch):
    from app.core import background, scratch
    monkeypatch.setattr(background, "stopping", False)
    monkeypatch.setattr(scratch, "_reservations", {})
    from app.core import output_files, output_validation, trash
    monkeypatch.setattr(output_files, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(output_files, "COMMIT_JOURNAL_DIR", tmp_path / "config" / "pending-commits")
    monkeypatch.setattr(output_files, "TRASH_ROOTS_FILE", tmp_path / "config" / "trash-roots.json")
    monkeypatch.setattr(trash, "CONFIG_DIR", tmp_path / "config")
    work = tmp_path_factory.mktemp("quality-work")
    work.mkdir(exist_ok=True)
    monkeypatch.setattr(output_validation, "TRANSCODE_DIR", work)
