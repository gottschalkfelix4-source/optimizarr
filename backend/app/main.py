"""FastAPI application: API + the built web UI, served from one container."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import routes_jobs, routes_library, routes_system
from .config import CONFIG_DIR, TRANSCODE_DIR, load_settings, save_settings
from .core import hwaccel, scanner, worker
from .core.events import bus
from .db import engine, session_scope
from .models import Base, HistoryEntry, Job, JobState, MediaFile, FileState
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
        if stuck_jobs or stuck_files:
            s.add(HistoryEntry(
                level="warning", category="system",
                message=f"Nach Neustart aufgeraeumt: {len(stuck_jobs)} Job(s) neu eingereiht.",
            ))
            log.info("recovered %d orphaned jobs", len(stuck_jobs))


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Optimizarr %s starting", __version__)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)

    Base.metadata.create_all(engine())
    settings = load_settings(force=True)
    save_settings(settings)  # materialise defaults on first run

    bus.bind_loop(asyncio.get_running_loop())
    _recover_orphans()

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
        asyncio.create_task(detect())

    worker.queue_worker.start()
    worker.scheduler.start()

    if settings.library.scan_on_start:
        async def initial_scan() -> None:
            await asyncio.sleep(5)  # let hardware detection settle first
            with session_scope() as s:
                from .models import LibraryPath
                has_paths = s.query(LibraryPath).filter(LibraryPath.enabled.is_(True)).count()
            if has_paths and not scanner.state.running:
                await scanner.run_scan(trigger="startup")
        asyncio.create_task(initial_scan())

    try:
        yield
    finally:
        log.info("shutting down")
        scanner.cancel_scan()
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


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Interner Fehler: {type(exc).__name__}: {exc}"},
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
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
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
