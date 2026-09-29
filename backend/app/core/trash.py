"""Recorded originals only: retention, conflict detection and reversible restore.

Old date folders are deliberately never inferred to be owned by Optimizarr.
Each original has a durable manifest written before its last source name goes.
"""
from __future__ import annotations

import datetime as dt
import errno
import json
import os
import shutil
import stat
import threading
import time
import uuid
from pathlib import Path

from ..config import CONFIG_DIR
from . import durable

_lock = threading.RLock()


def records_dir() -> Path:
    return CONFIG_DIR / "trash-items"


def fingerprint(path: str | Path) -> list[int]:
    p = Path(path)
    st = p.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("Keine regulaere Datei.")
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


def matches(path: str | Path, expected: list[int] | None) -> bool:
    try:
        return expected is not None and fingerprint(path) == expected
    except (OSError, ValueError):
        return False


def record_path(item_id: str) -> Path:
    if len(item_id) != 32 or any(c not in "0123456789abcdef" for c in item_id):
        raise ValueError("Ungueltiger Papierkorb-Eintrag.")
    return records_dir() / f"{item_id}.json"


def read(item_id: str) -> dict:
    entry = json.loads(record_path(item_id).read_text())
    if entry.get("id") != item_id:
        raise ValueError("Ungueltiger Papierkorb-Eintrag.")
    return entry


def _copy_exclusive(source: str | Path, target: Path) -> None:
    # Exclusive creation protects even an unexpected file at our generated name.
    try:
        with open(source, "rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copystat(source, target)
    except FileExistsError:
        raise
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _secure_copy(source: str | Path, dest: Path) -> None:
    try:
        os.link(source, dest)
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP):
            raise
        _copy_exclusive(source, dest)
    with dest.open("rb") as stream:
        os.fsync(stream.fileno())
    durable.sync_dir(dest.parent)


def move_original(source: str, actual: str, dest: Path, replacement: str | None = None, original_info: dict | None = None) -> str:
    with _lock:
        source = str(Path(source).absolute())
        actual = str(Path(actual).absolute())
        original = fingerprint(actual)
        replacement_sig = fingerprint(replacement) if replacement else None
        dest = dest.parent.resolve() / dest.name
        item_id = uuid.uuid4().hex
        entry = {
            "id": item_id, "source": source, "path": str(dest.absolute()),
            "size": original[2], "trashed_at": time.time(), "state": "preparing",
            "replacement": replacement, "replacement_signature": replacement_sig,
            "original_signature": original, "original_info": original_info,
            "source_parent": str(Path(source).parent.resolve()),
        }
        path = record_path(item_id)
        durable.write_json(path, entry)
        try:
            _secure_copy(actual, dest)
            if not matches(actual, original):
                raise ValueError("Original wurde beim Verschieben veraendert.")
            entry.update(state="available", signature=fingerprint(dest))
            durable.write_json(path, entry)
            os.unlink(actual)
            durable.sync_dir(Path(actual).parent)
        except BaseException:
            # Only undo our copy while the original still exists unchanged.
            if matches(actual, original):
                if matches(dest, entry.get("signature")):
                    dest.unlink()
                path.unlink(missing_ok=True)
            raise
        return str(dest)


def conflict(entry: dict) -> str:
    if entry.get("state") == "restored" and not entry.get("reconciled"):
        return "" if matches(entry["source"], entry.get("restored_signature")) else "Das wiederhergestellte Original wurde veraendert."
    if entry.get("state") != "available":
        return "Unvollstaendiger Vorgang; manuelle Pruefung erforderlich."
    if not matches(entry["path"], entry.get("signature")):
        return "Papierkorb-Datei fehlt oder wurde veraendert."
    source = entry["source"]
    replacement = entry.get("replacement")
    # Same-name restoration can replace only the exact output we recorded.
    if os.path.lexists(source) and not (
        source == replacement and matches(source, entry.get("replacement_signature"))
    ):
        return "Am Originalpfad liegt bereits eine andere Datei."
    if replacement and os.path.lexists(replacement) and not matches(
        replacement, entry.get("replacement_signature")
    ):
        return "Die konvertierte Datei wurde inzwischen veraendert."
    if str(Path(source).parent.resolve()) != entry.get("source_parent", str(Path(source).parent.absolute())):
        return "Der Originalordner ist jetzt eine Verknuepfung."
    return ""


def listing(retention_days: int) -> dict:
    items = []
    with _lock:
        for path in sorted(records_dir().glob("*.json")):
            try:
                entry = read(path.stem)
                if entry.get("state") == "restored" and entry.get("reconciled"):
                    continue
                expires = entry["trashed_at"] + retention_days * 86400 if retention_days else None
                items.append({
                    "id": entry["id"], "source": entry["source"], "path": entry["path"],
                    "size": entry["size"], "trashed_at": _iso(entry["trashed_at"]),
                    "expires_at": _iso(expires) if expires else None,
                    "conflict": conflict(entry),
                })
            except (OSError, ValueError, KeyError, TypeError):
                continue  # Corrupt manifests never authorize removal.
    items.sort(key=lambda item: item["trashed_at"], reverse=True)
    return {"items": items, "total_size": sum(i["size"] for i in items)}


def _iso(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()


def purge(retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    removed = 0
    with _lock:
        for path in records_dir().glob("*.json"):
            try:
                entry = read(path.stem)
                if entry.get("state") != "available" or entry["trashed_at"] >= time.time() - retention_days * 86400:
                    continue
                if not matches(entry["path"], entry.get("signature")):
                    continue
                Path(entry["path"]).unlink()
                durable.sync_dir(Path(entry["path"]).parent)
                path.unlink()
                removed += 1
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return removed


def restore(item_id: str) -> dict:
    """Caller must prevent new jobs/scans for this file until DB reconciliation.

    The encoded file is retained with an .original marker, so even a later DB
    failure never destroys either version. An interrupted restore is visible
    as a conflict and cannot be purged automatically.
    """
    with _lock:
        entry = read(item_id)
        reason = conflict(entry)
        if reason:
            raise ValueError(reason)
        source = Path(entry["source"])
        source.parent.mkdir(parents=True, exist_ok=True)
        stage = source.with_name(f".optimizarr-restore-{item_id}")
        replacement = entry.get("replacement")
        kept = None
        _copy_exclusive(entry["path"], stage)
        try:
            reason = conflict(entry)
            if reason:
                raise ValueError(reason)
            if replacement and os.path.lexists(replacement):
                output = Path(replacement)
                kept = output.with_name(f"{output.stem}.restored-{item_id[:8]}.original{output.suffix}")
                _secure_copy(output, kept)
            entry.update(state="restoring", kept_output=str(kept) if kept else None)
            durable.write_json(record_path(item_id), entry)
            # Check again after copying; external library managers may have acted.
            if replacement and os.path.lexists(replacement) and not matches(replacement, entry.get("replacement_signature")):
                raise ValueError("Die konvertierte Datei wurde inzwischen veraendert.")
            if source == Path(replacement or "") and os.path.lexists(source):
                os.replace(stage, source)
            else:
                os.link(stage, source)  # Atomic no-clobber install.
                stage.unlink()
            durable.sync_dir(source.parent)
            if replacement and str(source) != replacement and matches(replacement, entry.get("replacement_signature")):
                os.unlink(replacement)
                durable.sync_dir(Path(replacement).parent)
            entry.update(state="restored", restored_signature=fingerprint(source))
            durable.write_json(record_path(item_id), entry)
            return entry
        finally:
            stage.unlink(missing_ok=True)


def finish_restore(entry: dict) -> None:
    with _lock:
        if matches(entry["path"], entry.get("signature")):
            Path(entry["path"]).unlink()
            durable.sync_dir(Path(entry["path"]).parent)
        entry["reconciled"] = True
        durable.write_json(record_path(entry["id"]), entry)
        # Keep the small restored record for audit and interrupted DB recovery.
