"""Inspect recorded originals and restore them without losing either version."""
import asyncio
import os
from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from ..config import load_settings
from ..db import session_scope
from ..models import MediaFile, Job, JobState, FileState, HistoryEntry, LibraryPath
from ..core import trash, scanner, maintenance
from ..core.events import bus

router = APIRouter()


@router.get("/trash")
def list_trash():
    return trash.listing(load_settings().output.trash_retention_days)


def _restore(item_id: str) -> dict:
    with session_scope() as session:
        entry = trash.read(item_id)
        paths = [entry["source"], entry.get("replacement") or entry["source"]]
        rows = session.scalars(select(MediaFile).where(MediaFile.path.in_(paths))).all()
        ids = [row.id for row in rows]
        if session.scalar(select(Job.id).where(Job.file_id.in_(ids), Job.state.in_([JobState.QUEUED.value, JobState.RUNNING.value])).limit(1)):
            raise ValueError("Fuer diese Datei wartet oder laeuft ein Job. Bitte zuerst abbrechen.")
        if len(rows) > 1:
            raise ValueError("Original und Ausgabe sind separat erfasst. Bitte den Konflikt zuerst in der Bibliothek klaeren.")
    # No SQLite write transaction while copying a potentially huge original.
    # maintenance.exclusive blocks new claims and enqueue operations meanwhile.
    if entry.get("state") == "restored" and not entry.get("reconciled"):
        if not trash.matches(entry["source"], entry.get("restored_signature")):
            raise ValueError("Das wiederhergestellte Original wurde inzwischen veraendert.")
    else:
        entry = trash.restore(item_id)
    with session_scope() as session:
        rows = session.scalars(select(MediaFile).where(MediaFile.id.in_(ids))).all()
        if not rows:
            library = next((lib for lib in sorted(session.scalars(select(LibraryPath)).all(), key=lambda lib: -len(lib.path)) if entry["source"].startswith(lib.path.rstrip("/") + "/")), None)
            row = MediaFile(path=entry["source"], library_id=library.id if library else None)
            session.add(row)
            rows = [row]
        for row in rows:
            if row.path not in paths:
                raise ValueError("Bibliothekseintrag wurde inzwischen verschoben; bitte erneut pruefen.")
            row.path = entry["source"]
            row.size = os.path.getsize(row.path)
            row.mtime = os.path.getmtime(row.path)
            # Restore recorded metadata and prevent automatic re-encoding of
            # a version the user explicitly chose to bring back.
            row.state = FileState.IGNORED.value
            row.ignored = True
            row.plan = None
            if entry.get("original_info"):
                from ..core.encoder import _apply_probe
                from ..core.ffmpeg import MediaInfo
                _apply_probe(row, MediaInfo(**entry["original_info"]))
            else:
                row.video_codec = ""
            row.converted_at = None
            row.original_size = 0
            row.measured_vmaf = None
            row.quality_metric = None
            row.quality_value = None
            row.estimated_saving_bytes = 0
            row.estimated_saving_pct = 0
            row.error = ""
            row.decision_reason = "Original wiederhergestellt; automatisch ignoriert. Fuer erneute Analyse freigeben."
        session.add(HistoryEntry(level="info", category="system", message=f'Original wiederhergestellt: {entry["source"]}'))
    trash.finish_restore(entry)
    bus.publish("library.changed", {})
    bus.publish("trash.changed", {})
    return {"ok": True, "source": entry["source"], "kept_output": entry.get("kept_output")}


@router.post("/trash/{item_id}/restore")
async def restore_original(item_id: str):
    try:
        with maintenance.exclusive():
            if scanner.state.running:
                raise ValueError("Bitte den laufenden Scan zuerst beenden.")
            task = asyncio.create_task(asyncio.to_thread(_restore, item_id))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A disconnected request must not release maintenance while
                # its filesystem thread is still copying/replacing files.
                await task
                raise
    except FileNotFoundError as exc:
        raise HTTPException(404, "Papierkorb-Eintrag oder Datei nicht gefunden.") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(409, f"Wiederherstellung nicht moeglich: {exc}") from exc
