"""Small, durable JSON records: errors must reach the caller before mutation."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sync_dir(path.parent.parent)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)
