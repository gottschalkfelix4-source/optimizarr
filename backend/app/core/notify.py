"""Webhook notifications for finished jobs and scans.

Listens on the event bus and POSTs a small JSON document to the configured
URL.  The body carries ``event``, ``title``, ``message`` and ``data``, plus
``content`` and ``text`` with a one-line summary so Discord (``content``) and
Slack/Mattermost-style (``text``) webhooks show something readable without an
adapter.  A notification that cannot be delivered is logged and dropped - it
must never hold up an encode.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from ..config import load_settings
from ..db import session_scope
from ..models import Job, MediaFile
from .events import EventBus

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0

# job.finished states -> (settings flag, title prefix)
_JOB_STATES = {
    "done": ("notify_on_job_done", "Konvertierung abgeschlossen"),
    "failed": ("notify_on_job_failed", "Konvertierung fehlgeschlagen"),
    # The encode ran, the result was discarded (not smaller, quality too low):
    # nothing changed on disk, which is what "failed" subscribers want to know.
    "rejected": ("notify_on_job_failed", "Ergebnis verworfen"),
}


def _valid_url(url: str) -> bool:
    return url.startswith(("http://", "https://"))


async def send(
    event: str, title: str, message: str, data: dict[str, Any], url: str | None = None,
) -> tuple[bool, str]:
    """POST one notification.  Returns (ok, German status text); never raises."""
    target = url if url is not None else load_settings().notifications.webhook_url
    if not target:
        return False, "Keine Webhook-URL eingetragen."
    if not _valid_url(target):
        return False, "Die Webhook-URL muss mit http:// oder https:// beginnen."
    summary = f"{title}: {message}" if message else title
    body = {
        "event": event, "title": title, "message": message, "data": data,
        "content": summary[:2000], "text": summary,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(target, json=body)
    except httpx.HTTPError as exc:
        # The URL is a secret (tokens live in it) - log the kind of error only.
        log.warning("notification %s could not be delivered: %s", event, type(exc).__name__)
        return False, f"Webhook nicht erreichbar ({type(exc).__name__})."
    if response.status_code >= 400:
        log.warning("notification %s rejected with HTTP %s", event, response.status_code)
        return False, f"Webhook antwortete mit HTTP {response.status_code}."
    return True, f"Gesendet (HTTP {response.status_code})."


def _file_name(job_id: Any, file_id: Any) -> str:
    with session_scope() as s:
        media = s.get(MediaFile, file_id) if file_id else None
        if media is None and job_id:
            job = s.get(Job, job_id)
            media = job.file if job is not None else None
        return Path(media.path).name if media is not None else ""


async def build(event: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]] | None:
    """Turn a bus event into (event, title, message, data) - or None if not wanted."""
    kind = event.get("type")
    data = dict(event.get("data") or {})
    cfg = load_settings().notifications
    if not cfg.webhook_url:
        return None

    if kind == "job.finished":
        rule = _JOB_STATES.get(str(data.get("state")))
        if rule is None or not getattr(cfg, rule[0]):
            return None
        name = await asyncio.to_thread(_file_name, data.get("job_id"), data.get("file_id"))
        if name:
            data["name"] = name
        title = f"{rule[1]}: {name}" if name else rule[1]
        return kind, title, str(data.get("message") or ""), data

    if kind == "scan.finished" and cfg.notify_on_scan_done:
        if data.get("error"):
            return kind, "Scan abgebrochen", str(data["error"]), data
        message = (
            f"{data.get('analyzed', 0)} Dateien analysiert, "
            f"{data.get('candidates', 0)} Kandidaten gefunden."
        )
        return kind, "Scan abgeschlossen", message, data
    return None


async def run(bus: EventBus) -> None:
    """Forward bus events to the webhook until cancelled."""
    queue = bus.subscribe()
    pending: set[asyncio.Task] = set()
    try:
        while True:
            event = await queue.get()
            if event.get("type") not in ("job.finished", "scan.finished"):
                continue
            try:
                built = await build(event)
            except Exception:
                log.warning("could not prepare a notification", exc_info=True)
                continue
            if built is None:
                continue
            # Delivery runs on its own so a slow webhook never backs up the queue.
            task = asyncio.create_task(send(*built))
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        bus.unsubscribe(queue)
        for task in pending:
            task.cancel()
