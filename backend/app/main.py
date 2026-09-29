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
from .config import AppSettings, CONFIG_DIR, TRANSCODE_DIR, load_settings, save_settings
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
            job.fps = 0.0
            job.speed = 0.0
            job.eta_seconds = 0
            job.current_size = 0
            job.started_at = None
            job.finished_at = None
            job.log = (job.log or "") + "[neustart] Job wurde nach einem Neustart neu eingereiht.\n"
        s.flush()
        # Every file that still has a job waiting belongs in "queued" - not
        # only those of the jobs reset just now.
        queued_files = {
            file_id for (file_id,) in
            s.query(Job.file_id).filter(Job.state == JobState.QUEUED.value).all()
        }
        stuck_files = s.query(MediaFile).filter(
            MediaFile.state.in_([FileState.ENCODING.value, FileState.ANALYZING.value])
        ).all()
        for media in stuck_files:
            if media.id in queued_files:
                media.state = FileState.QUEUED.value
            elif media.plan:
                media.state = FileState.CANDIDATE.value
            else:
                # Never analysed: "candidate" would put it up for auto-queue
                # without a plan.
                media.state = (
                    FileState.PROBED.value if media.video_codec else FileState.NEW.value
                )
        # A scan cut off by the restart would otherwise read "running" forever.
        for run in s.query(ScanRun).filter(ScanRun.state == "running").all():
            run.state = "failed"
            run.error = "Durch Neustart unterbrochen"
            run.finished_at = utcnow()
        if stuck_jobs or stuck_files:
            s.add(HistoryEntry(
                level="warning", category="system",
                message=(
                    f"Nach Neustart aufgeraeumt: {len(stuck_jobs)} Job(s) neu eingereiht, "
                    f"{len(stuck_files)} Datei(en) zurueckgesetzt."
                ),
            ))
            log.info(
                "recovered %d orphaned job(s) and %d file(s)", len(stuck_jobs), len(stuck_files)
            )


def _clean_transcode_dir() -> None:
    """Remove temporary encodes and probe folders a crash left behind.

    Runs before the worker starts, so nothing in here can still be in use.
    Only our own prefix is touched - the directory may be shared.

    The library side is handled too, without walking it (tens of terabytes,
    disks spun down): a commit cut off half-way is rolled back from its journal
    entry, and staging copies from before the journal existed are only looked
    for next to files that still have a job waiting - the only ones a commit
    can have been interrupted on.
    """
    from .core import encoder

    try:
        rolled_back = encoder.recover_interrupted_commits()
        if rolled_back:
            log.warning("rolled back %d interrupted replacement(s)", rolled_back)
    except Exception:
        log.exception("could not roll back interrupted replacements")
    try:
        with session_scope() as s:
            folders = {
                os.path.dirname(path) for (path,) in
                s.query(MediaFile.path).join(Job, Job.file_id == MediaFile.id)
                .filter(Job.state.in_([JobState.QUEUED.value, JobState.RUNNING.value])).all()
            }
        swept = encoder.sweep_stale_staging(sorted(folders))
        if swept:
            log.info("removed %d leftover staging file(s) from the library", swept)
    except Exception:
        log.exception("could not sweep leftover staging files")

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


#: The recycle folder every installation got before the per-library default.
LEGACY_TRASH_DIR = "/config/trash"
TRASH_MIGRATION_MARKER = "trash-dir-migration.done"


def _migrate_trash_dir(settings: AppSettings) -> bool:
    """Once: move a stored ``/config/trash`` over to the per-library default.

    ``/config/trash`` was the default, and the first start writes every default
    into the database - so it is stored on every older installation, chosen
    or not.  It lies on the appdata share: every recycled original was copied
    across filesystems instead of renamed.  Empty means
    ``<library>/.optimizarr-trash`` on the same filesystem.  What is already in
    ``/config/trash`` stays there; ``purge_trash`` keeps cleaning that folder.

    Returns whether the setting changed (the caller saves it).  Runs once, so
    a later deliberate choice of ``/config/trash`` is left alone.
    """
    marker = CONFIG_DIR / TRASH_MIGRATION_MARKER
    try:
        if marker.exists():
            return False
    except OSError:
        return False
    changed = settings.output.trash_dir.rstrip("/") == LEGACY_TRASH_DIR
    if changed:
        settings.output.trash_dir = ""
        with session_scope() as s:
            s.add(HistoryEntry(
                level="info", category="system",
                message=(
                    "Papierkorb umgestellt: statt /config/trash liegt er jetzt als "
                    ".optimizarr-trash im jeweiligen Bibliotheksordner (gleiches Dateisystem, "
                    "kein Kopieren mehr). Bereits vorhandene Dateien in /config/trash bleiben "
                    "dort und werden nach Ablauf der Aufbewahrungszeit geloescht."
                ),
            ))
        log.info("trash folder moved from %s to the per-library default", LEGACY_TRASH_DIR)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(utcnow().isoformat() + "\n")
    except OSError as exc:
        log.warning("could not write %s: %s", marker, exc)
    return changed


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
    from .migrations import upgrade
    upgrade(engine())
    settings = load_settings(force=True)
    if reset_auth_requested() and settings.security.auth_enabled:
        settings.security.auth_enabled = False
        log.warning("OPTIMIZARR_RESET_AUTH is set: login switched off. "
                    "Set a new password in the settings and remove the variable.")
    _migrate_trash_dir(settings)
    save_settings(settings)  # materialise defaults on first run

    bus.bind_loop(asyncio.get_running_loop())
    _recover_orphans()
    _clean_transcode_dir()

    # Samples from the old GPU quality handling go before the first fit.
    try:
        await asyncio.to_thread(worker.reset_legacy_hw_samples, CONFIG_DIR)
    except Exception:
        log.warning("could not clean up the old GPU learning samples", exc_info=True)

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

from .api import routes_trash
app.include_router(routes_trash.router, prefix="/api", tags=["trash"])

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
