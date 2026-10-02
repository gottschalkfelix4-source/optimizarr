"""Replacing an original: never both gone, never a stranger overwritten."""
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(Path(tempfile.gettempdir()) / "optimizarr-pytest/config"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db
from app.config import AppSettings
from app.core import output_files as encoder, planner, trash
from app.core.ffmpeg import MediaInfo
from app.models import Base, LibraryPath


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(encoder, "COMMIT_JOURNAL_DIR", tmp_path / "config" / "pending-commits", raising=False)
    monkeypatch.setattr(encoder, "TRASH_ROOTS_FILE", tmp_path / "config" / "trash-roots.json", raising=False)
    monkeypatch.setattr(encoder, "CONFIG_DIR", tmp_path / "config", raising=False)
    yield
    engine.dispose()


def settings(action="delete", mode="replace", trash_dir=""):
    cfg = AppSettings()
    cfg.output.mode = mode
    cfg.output.original_action = action
    cfg.output.trash_dir = trash_dir
    cfg.output.set_permissions = False
    return cfg


def library(tmp_path):
    lib = tmp_path / "lib"
    folder = lib / "Filme" / "Film (2020)"
    folder.mkdir(parents=True)
    source = folder / "Film.mkv"
    source.write_bytes(b"original")
    temp = tmp_path / "transcode.mkv"
    temp.write_bytes(b"encoded")
    return lib, source, temp


def commit(source, temp, cfg, **kw):
    return encoder._commit_output(
        str(source), str(temp), planner.EncodePlan(container="mkv"), cfg,
        MediaInfo(path=str(source)), **kw,
    )


def leftovers(folder):
    return sorted(p.name for p in folder.iterdir()
                  if p.name.startswith((".optimizarr-staging", ".optimizarr-backup")))


def trash_files(root):
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def signature(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


# --- success paths --------------------------------------------------------- #

@pytest.mark.parametrize("action", ["delete", "trash", "keep"])
def test_successful_replace_for_every_original_action(tmp_path, action):
    lib, source, temp = library(tmp_path)
    cfg = settings(action)
    target = commit(source, temp, cfg, source_signature=signature(source), library_root=str(lib))
    assert Path(target).read_bytes() == b"encoded"
    assert leftovers(source.parent) == []
    kept = source.parent / "Film.original.mkv"
    trashed = trash_files(lib / encoder.TRASH_DIRNAME)
    if action == "keep":
        assert kept.read_bytes() == b"original"
    else:
        assert not kept.exists()
    if action == "trash":
        assert [p.read_bytes() for p in trashed] == [b"original"]
        # The library layout is mirrored below the date folder.
        assert "Filme/Film (2020)/Film.mkv" in str(trashed[0])
    else:
        assert trashed == []
    assert not any(encoder.COMMIT_JOURNAL_DIR.glob("*.json"))


def test_staging_is_flushed_before_the_original_is_touched(tmp_path, monkeypatch):
    lib, source, temp = library(tmp_path)
    events = []
    real_fsync, real_secure = encoder._fsync_file, encoder._secure_original
    monkeypatch.setattr(encoder, "_fsync_file", lambda p: (events.append("fsync"), real_fsync(p)))
    monkeypatch.setattr(encoder, "_secure_original",
                        lambda s, b: (events.append("touch original"), real_secure(s, b))[1])
    commit(source, temp, settings("delete"))
    assert events.index("fsync") < events.index("touch original")


# --- a failing swap must leave the original ------------------------------- #

@pytest.mark.parametrize("action", ["delete", "trash", "keep"])
@pytest.mark.parametrize("links", [True, False])
def test_failing_replace_keeps_the_original_in_every_mode(tmp_path, monkeypatch, action, links):
    lib, source, temp = library(tmp_path)
    real_replace = os.replace

    def failing_replace(src, dst, *a, **kw):
        if Path(src).name.startswith(encoder.STAGING_PREFIX):
            raise OSError(5, "I/O error")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(encoder.os, "replace", failing_replace)
    if not links:
        # Filesystem without hard links: the rename fallback must be undone too.
        monkeypatch.setattr(encoder.os, "link", lambda *a: (_ for _ in ()).throw(OSError(1, "EPERM")))
    with pytest.raises(OSError):
        commit(source, temp, settings(action), library_root=str(lib))
    assert source.read_bytes() == b"original"
    assert leftovers(source.parent) == []
    assert not (source.parent / "Film.original.mkv").exists()
    assert trash_files(lib / encoder.TRASH_DIRNAME) == []


@pytest.mark.parametrize("action", ["delete", "trash"])
def test_failure_after_the_swap_brings_the_original_back(tmp_path, monkeypatch, action):
    lib, source, temp = library(tmp_path)
    if action == "delete":
        real_unlink = os.unlink

        def failing_unlink(path, *a, **kw):
            if Path(path).name.startswith(encoder.BACKUP_PREFIX):
                raise PermissionError(13, "denied")
            return real_unlink(path, *a, **kw)

        monkeypatch.setattr(encoder.os, "unlink", failing_unlink)
    else:
        monkeypatch.setattr(trash, "_secure_copy", lambda *a: (_ for _ in ()).throw(OSError(28, "full")))
    with pytest.raises(OSError):
        commit(source, temp, settings(action), library_root=str(lib))
    assert source.read_bytes() == b"original"
    assert leftovers(source.parent) == []


@pytest.mark.parametrize("action", ["delete", "trash", "keep"])
def test_new_container_is_taken_back_out_when_the_original_cannot_move(tmp_path, monkeypatch, action):
    lib = tmp_path / "lib"
    lib.mkdir()
    source = lib / "Film.avi"
    source.write_bytes(b"original")
    temp = tmp_path / "t.mkv"
    temp.write_bytes(b"encoded")
    real_unlink, real_rename = os.unlink, os.rename

    def refuse(path, *a, **kw):
        if Path(path) == source:
            raise PermissionError(13, "denied")
        return (real_unlink if not a else real_rename)(path, *a, **kw)

    monkeypatch.setattr(encoder.os, "unlink", refuse)
    monkeypatch.setattr(encoder.os, "rename", refuse)
    with pytest.raises(OSError):
        commit(source, temp, settings(action), library_root=str(lib))
    assert source.read_bytes() == b"original"
    assert not (lib / "Film.mkv").exists()
    assert leftovers(lib) == []


# --- the original changed underneath the encode --------------------------- #

@pytest.mark.parametrize("action", ["delete", "trash", "keep"])
def test_changed_source_is_never_replaced(tmp_path, action):
    lib, source, temp = library(tmp_path)
    before = signature(source)
    time.sleep(0.01)
    source.write_bytes(b"a newer release, bigger")
    with pytest.raises(encoder.SourceChangedError, match="veraendert"):
        commit(source, temp, settings(action), source_signature=before, library_root=str(lib))
    assert source.read_bytes() == b"a newer release, bigger"
    assert leftovers(source.parent) == []
    assert not (source.parent / "Film.original.mkv").exists()
    assert trash_files(lib / encoder.TRASH_DIRNAME) == []


# --- keep: an earlier kept original is sacred ----------------------------- #

def test_keep_never_overwrites_an_existing_original(tmp_path):
    lib, source, temp = library(tmp_path)
    first = source.parent / "Film.original.mkv"
    first.write_bytes(b"kept last year")
    commit(source, temp, settings("keep"))
    assert first.read_bytes() == b"kept last year"
    assert (source.parent / "Film.1.original.mkv").read_bytes() == b"original"
    assert source.read_bytes() == b"encoded"


def test_numbered_originals_are_still_skipped_by_the_scanner_rule():
    # scanner.walk_paths skips names whose stem ends in ".original".
    name = encoder.free_backup_path("/nonexistent/Film.mkv").name
    assert Path(name).stem.endswith(".original")


# --- the recycle folder ------------------------------------------------------ #

def test_empty_trash_dir_means_the_library_root(tmp_path):
    lib, source, temp = library(tmp_path)
    with db.session_scope() as s:
        s.add(LibraryPath(path=str(lib), name="lib"))
    dest = Path(encoder._move_to_trash(str(source), settings("trash")))
    assert dest.is_relative_to(lib / encoder.TRASH_DIRNAME)
    assert dest.read_bytes() == b"original"


def test_trash_falls_back_to_the_file_folder_without_a_library(tmp_path):
    _, source, _ = library(tmp_path)
    dest = Path(encoder._move_to_trash(str(source), settings("trash")))
    assert dest.is_relative_to(source.parent / encoder.TRASH_DIRNAME)


def test_trash_on_another_filesystem_needs_room_first(tmp_path, monkeypatch):
    lib, source, temp = library(tmp_path)
    other = tmp_path / "elsewhere"
    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if str(path).startswith(str(other)) or Path(path) == tmp_path:
            fields = {k: getattr(st, k) for k in dir(st) if k.startswith("st_")}
            fields["st_dev"] = st.st_dev + 1
            return SimpleNamespace(**fields)
        return st

    monkeypatch.setattr(encoder.os, "stat", fake_stat)
    monkeypatch.setattr(encoder.shutil, "disk_usage", lambda p: SimpleNamespace(free=10, total=0, used=0))
    with pytest.raises(OSError, match="Papierkorb"):
        commit(source, temp, settings("trash", trash_dir=str(other)))
    assert source.read_bytes() == b"original"
    assert leftovers(source.parent) == []


def test_trash_folders_get_the_configured_permissions(tmp_path):
    _, source, _ = library(tmp_path)
    cfg = settings("trash", trash_dir=str(tmp_path / "trash"))
    cfg.output.set_permissions = True
    cfg.output.file_mode = "0664"
    dest = Path(encoder._move_to_trash(str(source), cfg))
    assert stat.S_IMODE(os.stat(dest.parent).st_mode) == 0o775
    assert stat.S_IMODE(os.stat(tmp_path / "trash").st_mode) == 0o775


def _old_trash_file(root, name="x.mkv", stamp="2020-01-01"):
    path = root / stamp / "folder" / name
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")
    os.utime(path, (0, 0))
    return path


def test_purge_preserves_unrecorded_files_even_in_known_trash_roots(tmp_path):
    lib_a, lib_b = tmp_path / "a", tmp_path / "b"
    lib_a.mkdir()
    lib_b.mkdir()
    with db.session_scope() as s:
        s.add(LibraryPath(path=str(lib_a), name="a"))
        s.add(LibraryPath(path=str(lib_b), name="b"))
    configured = tmp_path / "configured"
    old = [
        _old_trash_file(lib_a / encoder.TRASH_DIRNAME),
        _old_trash_file(lib_b / encoder.TRASH_DIRNAME),
        _old_trash_file(configured),
    ]
    cfg = settings("trash", trash_dir=str(configured))
    cfg.output.trash_retention_days = 14
    assert encoder.purge_trash(cfg) == 0
    assert all(p.exists() for p in old)


def test_purge_only_touches_the_dated_trash_folders(tmp_path):
    share = tmp_path / "share"
    film = share / "Film.mkv"
    film.parent.mkdir()
    film.write_bytes(b"precious")
    os.utime(film, (0, 0))
    cfg = settings("trash", trash_dir=str(share))  # a mistyped trash_dir
    cfg.output.trash_retention_days = 1
    encoder.purge_trash(cfg)
    assert film.exists()


def test_purge_finds_a_folder_trash_used_without_a_library(tmp_path):
    _, source, _ = library(tmp_path)
    cfg = settings("trash")
    cfg.output.trash_retention_days = 1
    dest = Path(encoder._move_to_trash(str(source), cfg))
    for record in trash.records_dir().glob("*.json"):
        entry = json.loads(record.read_text())
        entry["trashed_at"] = 0
        record.write_text(json.dumps(entry))
    assert encoder.purge_trash(cfg) == 1


# --- crash recovery ---------------------------------------------------------- #

def _journal(entry):
    encoder.COMMIT_JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (encoder.COMMIT_JOURNAL_DIR / "x.json").write_text(json.dumps(entry))


def test_crash_in_the_rename_window_restores_the_original(tmp_path):
    folder = tmp_path / "lib"
    folder.mkdir()
    source = folder / "Film.mkv"
    backup = folder / ".optimizarr-backup-1234abcd-Film.mkv"
    backup.write_bytes(b"original")          # renamed away ...
    staging = folder / ".optimizarr-staging-1-1234abcd-Film.mkv"
    staging.write_bytes(b"encoded")          # ... new file not yet in place
    _journal({"staging": str(staging), "source": str(source), "target": str(source),
              "backup": str(backup), "replace": True, "action": "delete"})
    assert encoder.recover_interrupted_commits() == 1
    assert source.read_bytes() == b"original"
    assert not backup.exists() and not staging.exists()


def test_crash_after_the_swap_rolls_back_to_the_original(tmp_path):
    folder = tmp_path / "lib"
    folder.mkdir()
    source = folder / "Film.mkv"
    source.write_bytes(b"encoded")           # swap happened, DB never knew
    backup = folder / ".optimizarr-backup-1234abcd-Film.mkv"
    backup.write_bytes(b"original")
    staging = folder / ".optimizarr-staging-1-1234abcd-Film.mkv"
    _journal({"staging": str(staging), "source": str(source), "target": str(source),
              "backup": str(backup), "replace": True, "action": "trash", "version": 2,
              "source_fingerprint": encoder._fingerprint(backup), "output_fingerprint": encoder._fingerprint(source)})
    encoder.recover_interrupted_commits()
    assert source.read_bytes() == b"original"
    assert leftovers(folder) == []


def test_crash_with_only_the_safety_link_just_drops_the_link(tmp_path):
    folder = tmp_path / "lib"
    folder.mkdir()
    source = folder / "Film.mkv"
    source.write_bytes(b"original")
    backup = folder / ".optimizarr-backup-1234abcd-Film.mkv"
    os.link(source, backup)
    staging = folder / ".optimizarr-staging-1-1234abcd-Film.mkv"
    staging.write_bytes(b"encoded")
    _journal({"staging": str(staging), "source": str(source), "target": str(source),
              "backup": str(backup), "replace": True, "action": "delete"})
    encoder.recover_interrupted_commits()
    assert source.read_bytes() == b"original"
    assert leftovers(folder) == []
    assert not any(encoder.COMMIT_JOURNAL_DIR.glob("*.json"))


def test_old_staging_copies_are_swept_only_next_to_their_file(tmp_path):
    folder = tmp_path / "lib"
    folder.mkdir()
    (folder / "Film.mkv").write_bytes(b"x")
    redundant = folder / ".optimizarr-staging-7-0123abcd-Film.mkv"
    redundant.write_bytes(b"y")
    orphan = folder / ".optimizarr-staging-7-0123abcd-Other.mkv"   # maybe the last copy
    orphan.write_bytes(b"y")
    foreign = folder / ".staging-something"
    foreign.write_bytes(b"z")
    assert encoder.sweep_stale_staging([str(folder)]) == 1
    assert not redundant.exists()
    assert orphan.exists() and foreign.exists()


# Deterministic interleavings: all files are synthetic and isolated by tmp_path.
def _finished_journal(tmp_path, job_id):
    target = tmp_path / f"output-{job_id}.mkv"
    target.write_bytes(b"encoded")
    return encoder._journal_write({
        "reconciliation": {"job_id": job_id}, "phase": "filesystem_done",
        "target": str(target), "output_fingerprint": encoder._fingerprint(target),
    })


def test_finish_commit_other_job_removes_listed_manifest(tmp_path, monkeypatch):
    other = _finished_journal(tmp_path, 1)
    own = _finished_journal(tmp_path, 2)
    real_read = Path.read_text
    interleaved = False

    def read_after_other_finishes(path, *args, **kwargs):
        nonlocal interleaved
        if path == other and not interleaved:
            interleaved = True
            encoder.finish_commit(1)
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_after_other_finishes)
    encoder.finish_commit(2)
    assert interleaved
    assert not other.exists() and not own.exists()
    assert (tmp_path / "output-1.mkv").read_bytes() == b"encoded"
    assert (tmp_path / "output-2.mkv").read_bytes() == b"encoded"
    encoder.finish_commit(2)  # repeated completion is harmless


def test_finish_commit_same_job_finishes_before_unlink(tmp_path, monkeypatch):
    own = _finished_journal(tmp_path, 1)
    real_unlink = Path.unlink
    interleaved = False

    def unlink_after_other_finishes(path, *args, **kwargs):
        nonlocal interleaved
        if path == own and not interleaved:
            interleaved = True
            encoder.finish_commit(1)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_after_other_finishes)
    encoder.finish_commit(1)
    assert interleaved and not own.exists()


@pytest.mark.parametrize("operation", ["read", "unlink", "sync", "parse", "ownership"])
def test_finish_commit_real_errors_remain_visible(tmp_path, monkeypatch, operation):
    own = _finished_journal(tmp_path, 1)
    def denied(*args, **kwargs):
        raise PermissionError("synthetic permission failure")
    if operation == "read":
        monkeypatch.setattr(Path, "read_text", denied)
    elif operation == "unlink":
        monkeypatch.setattr(Path, "unlink", denied)
    elif operation == "sync":
        monkeypatch.setattr(encoder.durable, "sync_dir", denied)
    elif operation == "parse":
        own.write_text("{broken", encoding="utf-8")
    else:
        (tmp_path / "output-1.mkv").write_bytes(b"changed output")
    with pytest.raises((OSError, json.JSONDecodeError)):
        encoder.finish_commit(1)
    if operation != "sync":
        assert own.exists()


def test_finish_commit_preserves_other_jobs(tmp_path):
    other = _finished_journal(tmp_path, 1)
    encoder.finish_commit(2)
    assert other.exists()


def test_finish_commit_parallel_threads(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local
    paths = [_finished_journal(tmp_path, job_id) for job_id in (1, 2)]
    barrier, seen = Barrier(2), local()
    real_glob = Path.glob

    def listed_together(path, pattern):
        listed = list(real_glob(path, pattern))
        if path == encoder.COMMIT_JOURNAL_DIR and not getattr(seen, "listed", False):
            seen.listed = True
            barrier.wait(timeout=5)
        return iter(listed)

    monkeypatch.setattr(Path, "glob", listed_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(encoder.finish_commit, job_id) for job_id in (1, 2)]
        for future in futures:
            future.result(timeout=10)
    assert all(not path.exists() for path in paths)
