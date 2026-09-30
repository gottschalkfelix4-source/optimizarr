"""Conservative per-job scratch reservations shared by concurrent encodes."""
from __future__ import annotations
import shutil
import threading
from pathlib import Path

_lock = threading.RLock()
_reservations: dict[int, int] = {}
MARGIN = 256 * 1024**2


def required(input_size: int, predicted_size: int) -> int:
    # Forced/fallback encodes may exceed the prediction; leave room for checks.
    return max(input_size * 2, predicted_size, MARGIN) + MARGIN


def reserve(job_id: int, size: int, directory: Path, min_free_gb: float) -> str:
    with _lock:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(directory).free
        except OSError as exc:
            return f"Arbeitsverzeichnis nicht lesbar: {exc}"
        others = sum(value for key, value in _reservations.items() if key != job_id)
        if free - others - size < min_free_gb * 1024**3:
            return (
                f"Arbeitsverzeichnis: {free / 1024**3:.1f} GiB frei, "
                f"{others / 1024**3:.1f} GiB fuer aktive Jobs reserviert; "
                f"naechster Job braucht {size / 1024**3:.1f} GiB plus {min_free_gb:g} GiB Reserve."
            )
        _reservations[job_id] = size
        return ""


def release(job_id: int) -> None:
    with _lock:
        _reservations.pop(job_id, None)
