"""In-process pub/sub used to push live updates to the web UI."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import deque
from typing import Any

log = logging.getLogger(__name__)

MAX_QUEUE = 200
HISTORY = 50


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._recent: deque[dict[str, Any]] = deque(maxlen=HISTORY)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_QUEUE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def recent(self) -> list[dict[str, Any]]:
        return list(self._recent)

    def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Fan out to every listener.  Safe to call from any thread."""
        event = {
            "type": event_type,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "data": payload or {},
        }
        if event_type not in ("job.progress", "scan.progress"):
            self._recent.append(event)

        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is not None:
            self._dispatch(event)
        elif loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._dispatch, event)

    def _dispatch(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A slow client must never stall the encoder: drop the oldest.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


bus = EventBus()
