"""Durable media replacement, file ownership and recycle-bin operations."""
from __future__ import annotations
from dataclasses import asdict
import contextlib
import datetime as dt
import errno
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from ..config import AppSettings, CONFIG_DIR
from ..db import session_scope
from ..models import LibraryPath
from . import ffmpeg, durable, trash
from .planner import EncodePlan
from .encode_types import SourceChangedError
log = logging.getLogger(__name__)

#: Recycle folder created inside each library root when no trash_dir is set.
TRASH_DIRNAME = ".optimizarr-trash"
STAGING_PREFIX = ".optimizarr-staging-"
BACKUP_PREFIX = ".optimizarr-backup-"

#: One JSON file per commit in flight, so a crash half-way through replacing an
#: original can be rolled back on the next start (see recover_interrupted_commits).
COMMIT_JOURNAL_DIR = CONFIG_DIR / "pending-commits"
#: Every recycle folder ever used, so purge_trash finds them all again.
TRASH_ROOTS_FILE = CONFIG_DIR / "trash-roots.json"

#: Room left over when moving a file onto another filesystem.
_SPACE_MARGIN = 256 * 1024 * 1024

_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STAGING_RE = re.compile(r"^\.optimizarr-staging-\d+-[0-9a-f]{8}-(.+)$")


def _existing_ancestor(path: Path) -> Path:
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _ensure_room(dest_dir: Path, size: int, source: str, what: str) -> None:
    """Refuse a move onto another filesystem that would not fit.

    A move within one filesystem is a rename and needs no room at all; across
    filesystems it is a full copy, and running out half-way is the worst time.
    """
    try:
        anchor = _existing_ancestor(dest_dir)
        if os.stat(anchor).st_dev == os.stat(source).st_dev:
            return
        free = shutil.disk_usage(anchor).free
    except OSError as exc:
        raise OSError(f"Freier Platz fuer {what} ({dest_dir}) nicht ermittelbar: {exc}") from exc
    if free < size + _SPACE_MARGIN:
        raise OSError(
            f"Zu wenig Platz fuer {what} in {dest_dir}: {_fmt(free)} frei, "
            f"{_fmt(size)} benoetigt - nichts ersetzt."
        )


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #

def _apply_ownership(path: str, settings: AppSettings, directory: bool = False) -> None:
    """Match Unraid's expected 99:100 nobody/users unless configured otherwise."""
    if not settings.output.set_permissions:
        return
    try:
        mode = int(settings.output.file_mode, 8)
        if directory:
            # 0664 -> 0775: a folder needs x wherever it has r.
            mode |= (mode & 0o444) >> 2
        os.chmod(path, mode)
    except (OSError, ValueError) as exc:
        log.debug("chmod failed for %s: %s", path, exc)
    if hasattr(os, "chown") and os.geteuid() == 0:  # type: ignore[attr-defined]
        try:
            os.chown(path, settings.output.uid, settings.output.gid)
        except OSError as exc:
            log.debug("chown failed for %s: %s", path, exc)


def _make_dirs(path: Path, settings: AppSettings) -> None:
    """mkdir -p, giving every folder we create the configured owner and mode."""
    missing: list[Path] = []
    current = Path(path)
    while not current.exists() and current.parent != current:
        missing.append(current)
        current = current.parent
    for folder in reversed(missing):
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        _apply_ownership(str(folder), settings, directory=True)


# --------------------------------------------------------------------------- #
# Kept originals and the recycle folder
# --------------------------------------------------------------------------- #

def original_backup_path(source: str) -> Path:
    """Where a kept original is parked: ``Film.mkv`` -> ``Film.original.mkv``."""
    src = Path(source)
    return src.with_suffix(f".original{src.suffix}")


def free_backup_path(source: str) -> Path:
    """The first ``.original`` name not taken yet.

    ``Film.original.mkv``, then ``Film.1.original.mkv``, ``Film.2.original.mkv``
    ...  An earlier kept original is a different, older file and must never be
    overwritten.  The number goes before the marker because the scanner skips
    names whose stem ends in ``.original``.
    """
    first = original_backup_path(source)
    if not os.path.lexists(first):
        return first
    src = Path(source)
    counter = 1
    while True:
        candidate = src.with_name(f"{src.stem}.{counter}.original{src.suffix}")
        if not os.path.lexists(candidate):
            return candidate
        counter += 1


def library_root_for(path: str, library_id: int | None = None) -> str | None:
    """The library folder a file belongs to, or None."""
    try:
        with session_scope() as s:
            if library_id:
                lp = s.get(LibraryPath, library_id)
                if lp is not None and (
                    path == lp.path or path.startswith(lp.path.rstrip("/") + "/")
                ):
                    return lp.path
            roots = [lp.path for lp in s.query(LibraryPath).all()]
    except Exception:
        log.debug("could not look up the library of %s", path, exc_info=True)
        return None
    for root in sorted(roots, key=len, reverse=True):
        if path.startswith(root.rstrip("/") + "/"):
            return root
    return None


def trash_root(source: str, settings: AppSettings, library_root: str | None = None) -> Path:
    """The recycle folder for this file.

    Empty ``trash_dir``: ``<library>/.optimizarr-trash`` - on the same
    filesystem, so recycling is a rename and never a full copy.  Without a
    known library the file's own folder stands in.
    """
    if settings.output.trash_dir:
        return Path(settings.output.trash_dir)
    root = library_root or library_root_for(source)
    base = Path(root) if root else Path(source).parent
    return base / TRASH_DIRNAME


def _load_trash_roots() -> list[str]:
    try:
        data = json.loads(TRASH_ROOTS_FILE.read_text(encoding="utf-8"))
        return [str(p) for p in data if isinstance(p, str)]
    except (OSError, ValueError):
        return []


def _register_trash_root(root: Path) -> None:
    roots = _load_trash_roots()
    if str(root) in roots:
        return
    roots.append(str(root))
    try:
        TRASH_ROOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TRASH_ROOTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(roots), encoding="utf-8")
        os.replace(tmp, TRASH_ROOTS_FILE)
    except OSError as exc:
        log.warning("could not remember trash folder %s: %s", root, exc)


def _prepare_trash(
    source: str, settings: AppSettings, library_root: str | None = None,
) -> Path:
    """Pick (and create) the destination for a recycled original.

    Runs before the original is touched: a full or unwritable recycle folder
    has to stop the replacement, not strand the original half-way.
    """
    lib = library_root or library_root_for(source)
    root = trash_root(source, settings, lib)
    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    src = Path(source)
    rel: str | None = None
    if lib and source.startswith(lib.rstrip("/") + "/"):
        rel = os.path.relpath(src.parent, lib)
    if not rel or rel == "." or rel.startswith(".."):
        # Keep enough of the path to tell two "S01E01.mkv" apart.
        rel = src.parent.name or "root"
    dest_dir = root / stamp / rel
    _ensure_room(dest_dir, os.path.getsize(source), source, "den Papierkorb")
    _make_dirs(dest_dir, settings)
    _register_trash_root(root)
    dest = dest_dir / src.name
    counter = 1
    while os.path.lexists(dest):
        dest = dest_dir / f"{src.stem}.{counter}{src.suffix}"
        counter += 1
    return dest


def _move_file(src: str, dest: Path) -> None:
    """Rename, or copy-and-delete across filesystems - never a half copy left behind."""
    try:
        os.rename(src, dest)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    try:
        shutil.copy2(src, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise
    os.unlink(src)


def _move_to_trash(
    source: str, settings: AppSettings, library_root: str | None = None,
    from_path: str | None = None, dest: Path | None = None,
    replacement: str | None = None, original_info: dict | None = None,
) -> str:
    if dest is None:
        dest = _prepare_trash(source, settings, library_root)
    return trash.move_original(source, from_path or source, dest, replacement, original_info)


def _trash_roots(settings: AppSettings) -> list[Path]:
    """Every recycle folder that may hold something to purge."""
    candidates: list[Path] = []
    if settings.output.trash_dir:
        candidates.append(Path(settings.output.trash_dir))
    try:
        with session_scope() as s:
            candidates += [Path(lp.path) / TRASH_DIRNAME for lp in s.query(LibraryPath).all()]
    except Exception:
        log.debug("could not list library folders for the trash purge", exc_info=True)
    candidates += [Path(p) for p in _load_trash_roots()]
    candidates.append(CONFIG_DIR / "trash")  # the old default location
    seen: set[str] = set()
    roots: list[Path] = []
    for root in candidates:
        key = os.path.abspath(root)
        if key in seen or key == "/" or not root.is_dir():
            continue
        seen.add(key)
        roots.append(root)
    return roots


def purge_trash(settings: AppSettings) -> int:
    return trash.purge(settings.output.trash_retention_days)


def _target_path(source: str, plan: EncodePlan, settings: AppSettings) -> str:
    """Where the finished file should end up."""
    src = Path(source)
    suffix = f".{plan.container}"
    cfg = settings.output
    if cfg.mode == "sidecar":
        if not cfg.sidecar_suffix.strip():
            raise ValueError("Namenszusatz darf nicht leer sein.")
        return str(src.with_name(f"{src.stem}{cfg.sidecar_suffix}{suffix}"))
    if cfg.mode == "separate_dir" and not cfg.output_dir:
        raise ValueError("Separater Ausgabeordner fehlt.")
    if cfg.mode == "separate_dir" and cfg.output_dir:
        out_root = Path(cfg.output_dir)
        # Mirror the library layout underneath the output directory.
        try:
            with session_scope() as s:
                roots = [lp.path for lp in s.query(LibraryPath).all()]
            rel = None
            for root in sorted(roots, key=len, reverse=True):
                if source.startswith(root.rstrip("/") + "/"):
                    rel = os.path.relpath(src.parent, root)
                    break
            target_dir = out_root / rel if rel and rel != "." else out_root
        except Exception:
            target_dir = out_root
        target_dir.mkdir(parents=True, exist_ok=True)
        return str(target_dir / f"{src.stem}{suffix}")
    return str(src.with_suffix(suffix))



def _fsync_file(path: str | Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as exc:
        # Some FUSE mounts cannot fsync at all; that is not a reason to stop.
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
    finally:
        os.close(fd)


def _fsync_dir(path: str | Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _check_source_unchanged(source: str, expected: tuple[int, int] | None) -> None:
    """Refuse to replace an original that changed since the encode began.

    A download client or *arr upgrading the release mid-encode would otherwise
    have its new file thrown away for an encode of the old one.
    """
    if expected is None:
        return
    try:
        st = os.stat(source)
    except FileNotFoundError:
        raise SourceChangedError(
            "Quelldatei ist waehrend des Encodings verschwunden - Ergebnis verworfen, "
            "nichts ersetzt."
        ) from None
    size, mtime_ns = expected
    if st.st_size != size or st.st_mtime_ns != mtime_ns:
        raise SourceChangedError(
            f"Quelldatei wurde waehrend des Encodings veraendert ({_fmt(size)} -> "
            f"{_fmt(st.st_size)}) - Ergebnis verworfen, Original bleibt unberuehrt."
        )


def _journal_write(entry: dict[str, Any]) -> Path:
    path = COMMIT_JOURNAL_DIR / f"{uuid.uuid4().hex}.json"
    durable.write_json(path, entry)
    return path


def _fingerprint(path: str | Path) -> list[int] | None:
    """Identity of a regular file; never follow a substituted symlink."""
    import stat
    try:
        st = os.lstat(path)
        if stat.S_ISREG(st.st_mode):
            return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]
    except OSError:
        pass
    return None


def _owned(path: str | Path, fingerprint: list[int] | None) -> bool:
    return fingerprint is not None and _fingerprint(path) == fingerprint


def _publish_new(staging: Path, target: str) -> None:
    """Publish without replacing a target created by another writer."""
    try:
        os.link(staging, target)
    except FileExistsError:
        raise FileExistsError(f"Ziel {target} existiert bereits - nichts ersetzt.") from None
    except OSError as exc:
        # No portable atomic no-replace rename exists on all supported mounts.
        # Refuse instead of silently falling back to a destructive replace.
        raise OSError(f"Ziel kann nicht sicher ohne Ueberschreiben angelegt werden: {target}: {exc}") from exc
    staging.unlink()
    _fsync_dir(Path(target).parent)


def finish_commit(job_id: int) -> None:
    """Remove only this job's reconciled, unchanged output journal."""
    for path in COMMIT_JOURNAL_DIR.glob("*.json"):
        entry = json.loads(path.read_text(encoding="utf-8"))
        if (entry.get("reconciliation") or {}).get("job_id") == job_id:
            if entry.get("phase") != "filesystem_done" or not _owned(entry["target"], entry.get("output_fingerprint")):
                raise OSError("Ausgabe hat sich vor Abschluss der Verbuchung veraendert.")
            path.unlink()
            durable.sync_dir(path.parent)


def pending_commits() -> dict[str, set[int]]:
    result = {"jobs": set(), "files": set()}
    for path in COMMIT_JOURNAL_DIR.glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            data = entry.get("reconciliation") or {}
            for key, field in (("jobs", "job_id"), ("files", "file_id")):
                if type(data.get(field)) is int:
                    result[key].add(data[field])
        except (OSError, ValueError, AttributeError):
            log.error("unreadable commit journal preserved: %s", path)
    return result


def _commit_output(
    source: str, temp_out: str, plan: EncodePlan, settings: AppSettings,
    info: ffmpeg.MediaInfo,
    source_signature: tuple[int, int] | None = None,
    library_root: str | None = None,
    notes: list[str] | None = None,
    reconciliation: dict[str, Any] | None = None,
) -> str:
    """Put the finished file in its final place, safely.

    The new file is staged next to its destination and flushed to disk before
    the original is touched, and the original stays reachable under a second
    name until the new one has taken its place.  At no point are both gone.
    """
    target = _target_path(source, plan, settings)
    replacing = settings.output.mode == "replace"
    same = os.path.normcase(os.path.realpath(target)) == os.path.normcase(os.path.realpath(source))
    if os.path.lexists(target) and os.path.exists(source):
        same = same or os.path.samefile(target, source)
    if not replacing and same:
        raise ValueError("Ausgabedatei und Quelle sind identisch - Original bleibt unveraendert.")
    if not same and os.path.lexists(target):
        # Film.avi -> Film.mkv next to an unrelated Film.mkv: os.replace would
        # destroy that file without it ever reaching the trash. Existing sidecar
        # and separate-dir targets also require an explicit conflict resolution.
        raise FileExistsError(
            f"Ziel {target} existiert bereits und ist nicht die Quelle - nichts ersetzt."
        )
    target_dir = Path(target).parent
    target_dir.mkdir(parents=True, exist_ok=True)

    # Unique per call: two concurrent jobs can share a target name.
    staging = target_dir / f"{STAGING_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}-{Path(target).name}"
    action = settings.output.original_action if replacing else ""
    backup: Path | None = None
    if replacing and same:
        # Where the original stays reachable while its name gets the new file.
        backup = (
            free_backup_path(source) if action == "keep"
            else Path(source).with_name(f"{BACKUP_PREFIX}{uuid.uuid4().hex[:8]}-{Path(source).name}")
        )
    entry = {
        "staging": str(staging), "source": source, "target": target,
        "backup": str(backup) if backup else "", "replace": replacing,
        "action": action, "created": time.time(),
        "version": 2, "phase": "prepared", "source_fingerprint": _fingerprint(source),
        "reconciliation": reconciliation,
    }
    journal = _journal_write(entry)

    def checkpoint(phase: str) -> None:
        entry["phase"] = phase
        if phase == "staged":
            entry["output_fingerprint"] = _fingerprint(staging)
        durable.write_json(journal, entry)
    committed = False
    try:
        _stage_and_replace(
            source, temp_out, staging, target, settings,
            expected=source_signature, library_root=library_root, backup=backup, notes=notes,
            original_info={k: v for k, v in asdict(info).items() if k != "raw"},
            checkpoint=checkpoint,
        )
        checkpoint("filesystem_done")
        committed = True
    except BaseException as exc:
        # Never leave a full-size hidden copy in the library - but only while
        # the original is still in place.  Otherwise the staging copy is one
        # of the two files the library still has.
        if not replacing or os.path.lexists(source):
            staging.unlink(missing_ok=True)
        elif staging.exists():
            log.error(
                "commit of %s failed with the original away from its place - keeping %s "
                "and %s for manual recovery", source, staging, backup,
            )
            journal = None  # the next start rolls it back
        if isinstance(exc, Exception) and journal is not None and _roll_back_commit(entry):
            journal.unlink(missing_ok=True)
            durable.sync_dir(journal.parent)
            journal = None
        raise
    finally:
        if journal is not None and committed and reconciliation is None:
            journal.unlink(missing_ok=True)
            durable.sync_dir(journal.parent)
    return target


def _secure_original(source: str, backup: Path) -> str:
    """Give the original a second name before its own name is reused.

    A hard link costs nothing and keeps the original at ``source`` as well;
    filesystems without links get a rename, which leaves the name empty for the
    blink until the new file arrives.
    """
    try:
        os.link(source, backup)
        return "link"
    except OSError as exc:
        log.debug("hard link for %s failed (%s) - renaming instead", source, exc)
    os.rename(source, backup)
    return "rename"


def _restore_original(source: str, backup: Path) -> None:
    """Put the original back under its name (overwriting a new file there)."""
    os.replace(backup, source)
    _fsync_dir(Path(source).parent)


def _stage_and_replace(
    source: str, temp_out: str, staging: Path, target: str, settings: AppSettings,
    expected: tuple[int, int] | None = None,
    library_root: str | None = None,
    backup: Path | None = None,
    notes: list[str] | None = None,
    original_info: dict | None = None,
    checkpoint=None,
) -> None:
    # --- 1. stage the new file next to its destination, durable ----------- #
    _ensure_room(staging.parent, os.path.getsize(temp_out), temp_out, "die neue Datei")
    try:
        shutil.move(temp_out, str(staging))
    except OSError:
        shutil.copy2(temp_out, str(staging))
        try:
            os.unlink(temp_out)
        except OSError:
            pass

    _apply_ownership(str(staging), settings)
    if settings.output.preserve_mtime:
        try:
            src_stat = os.stat(source)
            os.utime(staging, (src_stat.st_atime, src_stat.st_mtime))
        except OSError:
            pass
    _fsync_file(staging)
    _fsync_dir(staging.parent)
    if checkpoint:
        checkpoint("staged")
    _check_source_unchanged(source, expected)

    if settings.output.mode != "replace":
        _publish_new(staging, target)
        if checkpoint:
            checkpoint("published")
        return

    # --- 2. everything that can refuse, before the original is touched --- #
    action = settings.output.original_action
    trash_dest = _prepare_trash(source, settings, library_root) if action == "trash" else None
    _check_source_unchanged(source, expected)

    same = os.path.abspath(target) == os.path.abspath(source)
    if same:
        # --- 3a. same name: original keeps a second name, then atomic swap - #
        if backup is None:
            backup = (
                free_backup_path(source) if action == "keep"
                else Path(source).with_name(f"{BACKUP_PREFIX}{uuid.uuid4().hex[:8]}-{Path(source).name}")
            )
        how = _secure_original(source, backup)
        try:
            if checkpoint:
                checkpoint("original_secured")
            os.replace(str(staging), target)
        except BaseException:
            if how == "link":
                backup.unlink(missing_ok=True)
            else:
                _restore_original(source, backup)
            raise
        _fsync_dir(Path(target).parent)
        if checkpoint:
            checkpoint("published")
        if action == "keep":
            return  # the backup name *is* the kept original
        try:
            if action == "delete":
                os.unlink(backup)
            else:
                _move_to_trash(source, settings, library_root, from_path=str(backup), dest=trash_dest, replacement=target, original_info=original_info)
        except BaseException as exc:
            # The original could not be put away: bring it back rather than
            # leave it under a hidden name.  The encode is lost, nothing else.
            log.error("could not dispose of the original %s: %s - restoring it", source, exc)
            if os.path.lexists(backup):
                _restore_original(source, backup)
            raise
        return

    # --- 3b. new name (Film.avi -> Film.mkv): place it, then clear the old - #
    _publish_new(staging, target)
    if checkpoint:
        checkpoint("published")
    try:
        if action == "trash":
            _move_to_trash(source, settings, library_root, dest=trash_dest, replacement=target, original_info=original_info)
        elif action == "delete":
            os.unlink(source)
        else:
            # Kept originals always get the ".original" marker - also when the
            # container changes and the name would not clash.  The scanner skips
            # the marker; without it the kept copy came back as a new candidate
            # on the next scan and was converted again.
            os.rename(source, free_backup_path(source))
    except BaseException as exc:
        if os.path.lexists(source):
            # Original untouched: take the new file back out so the library is
            # exactly as before, instead of holding both side by side.
            log.error("could not dispose of the original %s: %s - undoing", source, exc)
            with contextlib.suppress(OSError):
                os.unlink(target)
            raise
        # The original is gone but the new file is in place - that is a
        # finished conversion with a failed clean-up, not a failure.
        if notes is not None:
            notes.append(f"Original wurde weggeraeumt, Nacharbeit schlug fehl: {exc}")


def recover_interrupted_commits() -> int:
    """Roll back commits a crash or power cut interrupted.  Returns how many.

    Runs at start-up, before the worker.  A journal entry exists only while a
    commit is in flight, and the database never recorded its success - so the
    consistent state is the one before it: the original back under its name,
    our staging copy and safety links gone.  The job itself is re-queued by
    the orphan recovery and simply runs again.
    """
    if not COMMIT_JOURNAL_DIR.is_dir():
        return 0
    handled = 0
    for leftover in COMMIT_JOURNAL_DIR.glob("*.tmp"):
        leftover.unlink(missing_ok=True)
    for entry_path in sorted(COMMIT_JOURNAL_DIR.glob("*.json")):
        try:
            entry = json.loads(entry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.error("unreadable commit journal preserved: %s", entry_path)
            continue
        try:
            replay = entry.get("reconciliation")
            target_owned = _owned(entry.get("target", ""), entry.get("output_fingerprint"))
            irreversible = target_owned and not os.path.lexists(entry.get("backup") or "") and (
                not entry.get("replace") or not os.path.lexists(entry.get("source", ""))
                or os.path.abspath(entry["target"]) == os.path.abspath(entry["source"])
            )
            if replay and target_owned and (entry.get("phase") == "filesystem_done" or irreversible):
                from .encoder import reconcile_commit
                reconcile_commit(entry)
                done = True
            else:
                done = _roll_back_commit(entry)
        except Exception as exc:
            log.error("could not roll back interrupted commit %s: %s", entry, exc)
            done = False
        if done:
            entry_path.unlink(missing_ok=True)
            handled += 1
    return handled


def _roll_back_commit(entry: dict[str, Any]) -> bool:
    staging = Path(entry.get("staging") or "")
    source = str(entry.get("source") or "")
    target = str(entry.get("target") or "")
    backup = Path(entry["backup"]) if entry.get("backup") else None
    replacing = bool(entry.get("replace"))
    if not staging.name.startswith(STAGING_PREFIX) or not source:
        return True  # not ours - nothing to do

    if entry.get("version") == 2:
        output_fp = entry.get("output_fingerprint")
        original_fp = entry.get("source_fingerprint")
        target_exists = os.path.lexists(target)
        target_owned = _owned(target, output_fp)
        original_present = _owned(source, original_fp)
        if backup is not None and os.path.lexists(backup):
            if not _owned(backup, original_fp) or (os.path.lexists(source) and not original_present and not _owned(source, output_fp)):
                log.error("interrupted commit: changed source/backup; preserving every file: %s", entry)
                return False
            if original_present:
                backup.unlink()
            else:
                _restore_original(source, backup)
            original_present = True
        if target_exists and os.path.abspath(target) != os.path.abspath(source):
            if target_owned and (original_present or not replacing):
                os.unlink(target)
            elif not target_owned:
                # An unrelated writer owns this path. Never delete it.
                log.warning("interrupted commit: preserving foreign target %s", target)
        if staging.exists():
            if not _owned(staging, output_fp) or (replacing and not original_present):
                return False
            staging.unlink()
        if replacing and not original_present and target_owned:
            log.error("interrupted commit requires manual reconciliation: %s", entry)
            return False
        return True

    if backup is not None and os.path.lexists(backup):
        if os.path.lexists(source) and os.path.samefile(source, backup):
            backup.unlink()  # only the safety link was made
        elif os.path.lexists(source):
            log.error("legacy journal cannot prove ownership of %s; preserving both files", source)
            return False
        else:
            _restore_original(source, backup)
        log.warning("interrupted commit: %s restored", source)
    # Legacy journals have no proof of ownership for a separate target.
    # Preserve it even when an interrupted conversion might have created it.
    if staging.exists():
        if replacing and not os.path.lexists(source):
            log.error("interrupted commit: %s is missing - keeping %s", source, staging)
            return False
        staging.unlink()
        log.info("interrupted commit: removed %s", staging)
    return True


def sweep_stale_staging(directories: list[str]) -> int:
    """Remove staging copies from before the commit journal existed.

    Only in the given folders (no walk over the library), only our own prefix,
    and only when the file it was meant to become is present - otherwise the
    staging copy may be the last copy of the new file and is left alone.
    """
    removed = 0
    for directory in sorted(set(directories)):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            match = _STAGING_RE.match(name)
            if not match:
                continue
            path = os.path.join(directory, name)
            if not os.path.lexists(os.path.join(directory, match.group(1))):
                log.warning("leaving %s: the file it belongs to is missing", path)
                continue
            try:
                os.unlink(path)
                removed += 1
            except OSError as exc:
                log.warning("could not remove %s: %s", path, exc)
    return removed



def _fmt(num: int | float) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024.0
    return f"{value:.1f} PiB"
