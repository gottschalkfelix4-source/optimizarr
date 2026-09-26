"""FastAPI application: API + the built web UI, served from one container."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import (
    routes_advisor, routes_jobs, routes_library, routes_movies, routes_series, routes_system,
)
from .config import CONFIG_DIR, TRANSCODE_DIR, load_settings, save_settings
from .core import hwaccel, notify, scanner, worker
from .core.events import bus
from .db import engine, session_scope
from .models import Base, HistoryEntry, Job, JobState, MediaFile, FileState, ScanRun, utcnow
from .security import SecurityMiddleware, reset_auth_requested
from .version import __version__

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("optimizarr")

STATIC_DIR = Path(os.environ.get("OPTIMIZARR_STATIC_DIR", "/app/static"))


def _recover_orphans() -> None:
    """A container restart leaves jobs stuck in 'running' - put them back."""
    with session_scope() as s:
        stuck_jobs = s.query(Job).filter(Job.state == JobState.RUNNING.value).all()
        for job in stuck_jobs:
            job.state = JobState.QUEUED.value
            job.progress = 0.0
            job.started_at = None
            job.log = (job.log or "") + "[neustart] Job wurde nach einem Neustart neu eingereiht.\n"
        stuck_files = s.query(MediaFile).filter(
            MediaFile.state.in_([FileState.ENCODING.value, FileState.ANALYZING.value])
        ).all()
        for media in stuck_files:
            media.state = FileState.QUEUED.value if any(
                j.file_id == media.id for j in stuck_jobs
            ) else FileState.CANDIDATE.value
        # A scan cut off by the restart would otherwise read "running" forever.
        for run in s.query(ScanRun).filter(ScanRun.state == "running").all():
            run.state = "failed"
            run.error = "Durch Neustart unterbrochen"
            run.finished_at = utcnow()
        if stuck_jobs or stuck_files:
            s.add(HistoryEntry(
                level="warning", category="system",
                message=f"Nach Neustart aufgeraeumt: {len(stuck_jobs)} Job(s) neu eingereiht.",
            ))
            log.info("recovered %d orphaned jobs", len(stuck_jobs))


def _clean_transcode_dir() -> None:
    """Remove temporary encodes and probe folders a crash left behind.

    Runs before the worker starts, so nothing in here can still be in use.
    Only our own prefix is touched - the directory may be shared.
    """
    if not TRANSCODE_DIR.is_dir():
        return
    removed = 0
    for entry in TRANSCODE_DIR.iterdir():
        if not entry.name.startswith("optimizarr-"):
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as exc:
            log.warning("could not remove leftover %s: %s", entry, exc)
    if removed:
        log.info("removed %d leftover temporary file(s) from %s", removed, TRANSCODE_DIR)


# Background tasks started by the lifespan.  asyncio only keeps weak
# references to tasks, so an unreferenced one can vanish mid-run.
_background: set[asyncio.Task] = set()


def _spawn(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Optimizarr %s starting", __version__)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)

    Base.metadata.create_all(engine())
    settings = load_settings(force=True)
    if reset_auth_requested() and settings.security.auth_enabled:
        settings.security.auth_enabled = False
        log.warning("OPTIMIZARR_RESET_AUTH is set: login switched off. "
                    "Set a new password in the settings and remove the variable.")
    save_settings(settings)  # materialise defaults on first run

    bus.bind_loop(asyncio.get_running_loop())
    _recover_orphans()
    _clean_transcode_dir()

    # Fit the predictor on whatever history already exists.
    try:
        await asyncio.to_thread(worker.refit_predictor)
    except Exception:
        log.warning("could not fit the prediction model on startup", exc_info=True)

    if settings.hardware.detect_on_start:
        async def detect() -> None:
            try:
                report = await hwaccel.detect(
                    settings.hardware.render_device, settings.hardware.qsv_low_power
                )
                log.info("hardware: %s", report.summary)
                bus.publish("hardware.detected", {"summary": report.summary})
            except Exception:
                log.warning("hardware detection failed", exc_info=True)
        _spawn(detect(), "optimizarr-detect")

    _spawn(notify.run(bus), "optimizarr-notify")
    worker.queue_worker.start()
    worker.scheduler.start()

    if settings.library.scan_on_start:
        async def initial_scan() -> None:
            await asyncio.sleep(5)  # let hardware detection settle first

            def enabled_paths() -> int:
                with session_scope() as s:
                    from .models import LibraryPath
                    return s.query(LibraryPath).filter(LibraryPath.enabled.is_(True)).count()

            has_paths = await asyncio.to_thread(enabled_paths)
            if has_paths and not scanner.state.running:
                await scanner.run_scan(trigger="startup")
        _spawn(initial_scan(), "optimizarr-initial-scan")

    try:
        yield
    finally:
        log.info("shutting down")
        scanner.cancel_scan()
        for task in list(_background):
            task.cancel()
        if _background:
            await asyncio.gather(*_background, return_exceptions=True)
        await worker.queue_worker.stop()
        await worker.scheduler.stop()


app = FastAPI(
    title="Optimizarr",
    version=__version__,
    description="KI-gestuetzte AV1-Optimierung fuer Medienbibliotheken",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# Basic auth (optional), the CSRF header and the WebSocket origin check.  Added
# before CORS so that CORS ends up outermost and answers preflights itself.
app.add_middleware(SecurityMiddleware, get_security=lambda: load_settings().security)

# The UI is served from the same origin; CORS only matters for `npm run dev`.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_system.router, prefix="/api", tags=["system"])
app.include_router(routes_library.router, prefix="/api", tags=["library"])
app.include_router(routes_jobs.router, prefix="/api", tags=["jobs"])
app.include_router(routes_series.router, prefix="/api", tags=["series"])
app.include_router(routes_movies.router, prefix="/api", tags=["movies"])
app.include_router(routes_advisor.router, prefix="/api", tags=["advisor"])


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    # The exception text can carry paths and internals - it goes to the log only.
    return JSONResponse(
        status_code=500,
        content={"detail": "Interner Fehler. Details stehen im Protokoll des Containers."},
    )


@app.get("/api/health")
def health() -> dict[str, object]:
    return {"status": "ok", "version": __version__}


# --------------------------------------------------------------------------- #
# Static frontend (built by Vite into /app/static)
# --------------------------------------------------------------------------- #

if STATIC_DIR.is_dir():
    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        """Serve the single-page app, letting the client router own the URLs."""
        if full_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        root = STATIC_DIR.resolve()
        candidate = (root / full_path).resolve()
        # "/..%2F..%2Fconfig/optimizarr.db" must not walk out of the bundle.
        if full_path and candidate.is_relative_to(root) and candidate.is_file():
            return FileResponse(str(candidate))
        index = STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(str(index))
        return JSONResponse(status_code=404, content={"detail": "UI nicht gebaut"})
else:  # pragma: no cover - dev mode
    @app.get("/")
    def dev_root() -> dict[str, str]:
        return {
            "message": "Optimizarr API laeuft. Das Frontend wird separat mit "
                       "'npm run dev' gestartet (Port 5173).",
            "docs": "/api/docs",
        }
