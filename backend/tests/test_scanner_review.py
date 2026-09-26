"""Scanner: missing libraries, scan lock, odd names, cancel, write lock, re-probe."""
import asyncio
import datetime as dt
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(Path(tempfile.gettempdir()) / "optimizarr-pytest/config"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db  # noqa: E402
from app.config import AppSettings  # noqa: E402
from app.core import analyzer, codecs, ffmpeg, hwaccel, scanner  # noqa: E402
from app.core.events import bus  # noqa: E402
from app.core.ffmpeg import MediaInfo  # noqa: E402
from app.models import (  # noqa: E402
    Base, FileState, HistoryEntry, Job, JobState, LearningSample, LibraryPath, MediaFile,
    ScanRun,
)

MiB = 1024 * 1024


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(config, "_cache", None)
    # The DV re-probe marker lives in the config dir - never the shared one.
    marker = tmp_path / "config" / "dolby-vision-reprobe.done"
    monkeypatch.setattr(scanner, "_dv_marker", lambda: marker, raising=False)
    monkeypatch.setattr(scanner, "_pending_analysis", set(), raising=False)
    monkeypatch.setattr(scanner.state, "running", False)
    yield
    engine.dispose()


@pytest.fixture()
def marker_done(tmp_path):
    path = tmp_path / "config" / "dolby-vision-reprobe.done"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("done")
    return path


def cfg() -> AppSettings:
    settings = AppSettings()
    settings.library.min_file_size_mb = 0
    return settings


def add_library(path, enabled=True) -> int:
    with db.session_scope() as s:
        lib = LibraryPath(path=str(path), name=Path(path).name, enabled=enabled)
        s.add(lib)
        s.flush()
        return lib.id


def new_run() -> int:
    with db.session_scope() as s:
        run = ScanRun(trigger="test", state="running")
        s.add(run)
        s.flush()
        return run.id


def add_row(path, lib_id, *, on_disk=True, **kw) -> int:
    values = dict(video_codec="hevc", state=FileState.CANDIDATE.value)
    if on_disk:
        st = Path(path).stat()
        values.update(size=st.st_size, mtime=st.st_mtime)
    values.update(kw)
    with db.session_scope() as s:
        row = MediaFile(path=str(path), library_id=lib_id, **values)
        s.add(row)
        s.flush()
        return row.id


def row(file_id) -> MediaFile:
    with db.session_scope() as s:
        return s.get(MediaFile, file_id)


def video(folder: Path, name: str, size: int = 2048) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(b"\0" * size)
    return p


# --------------------------------------------------------------------------- #
# M3: what counts as missing
# --------------------------------------------------------------------------- #

def test_files_of_a_disabled_library_are_not_marked_missing(tmp_path, marker_done):
    film = video(tmp_path / "off", "Film.mkv")
    lib_id = add_library(tmp_path / "off", enabled=False)
    file_id = add_row(film, lib_id)
    scanner._sync_disk_to_db(cfg(), new_run())
    assert row(file_id).state == FileState.CANDIDATE.value


def test_files_of_an_unreachable_library_are_not_marked_missing(tmp_path, marker_done):
    lib_id = add_library(tmp_path / "unmounted")
    file_id = add_row(tmp_path / "unmounted" / "Film.mkv", lib_id, on_disk=False)
    scanner._sync_disk_to_db(cfg(), new_run())
    assert row(file_id).state == FileState.CANDIDATE.value


def test_an_empty_mount_point_is_not_trusted(tmp_path, marker_done):
    (tmp_path / "share").mkdir()
    lib_id = add_library(tmp_path / "share")
    file_id = add_row(tmp_path / "share" / "Film.mkv", lib_id, on_disk=False)
    scanner._sync_disk_to_db(cfg(), new_run())
    assert row(file_id).state == FileState.CANDIDATE.value


def test_an_unreadable_folder_is_not_missing(tmp_path, marker_done, monkeypatch):
    root = tmp_path / "lib"
    video(root, "Other.mkv")
    lib_id = add_library(root)
    gone = add_row(root / "Locked" / "Film.mkv", lib_id, on_disk=False)
    real_walk = os.walk

    def walk(top, onerror=None, followlinks=False):
        err = PermissionError(13, "Permission denied")
        err.filename = str(root / "Locked")
        onerror(err)
        yield from real_walk(top, onerror=onerror, followlinks=followlinks)

    monkeypatch.setattr(scanner.os, "walk", walk)
    scanner._sync_disk_to_db(cfg(), new_run())
    assert row(gone).state == FileState.CANDIDATE.value


def test_a_deleted_file_in_a_reachable_library_is_missing(tmp_path, marker_done):
    root = tmp_path / "lib"
    video(root, "Stays.mkv")
    lib_id = add_library(root)
    gone = add_row(root / "Gone.mkv", lib_id, on_disk=False)
    scanner._sync_disk_to_db(cfg(), new_run())
    assert row(gone).state == FileState.MISSING.value


# --------------------------------------------------------------------------- #
# M3: coming back
# --------------------------------------------------------------------------- #

def test_an_unchanged_converted_file_comes_back_as_done(tmp_path, marker_done):
    film = video(tmp_path / "lib", "Film.mkv")
    lib_id = add_library(tmp_path / "lib")
    file_id = add_row(film, lib_id, video_codec="av1", state=FileState.MISSING.value,
                      converted_at=dt.datetime(2026, 1, 1))
    _, _, todo = scanner._sync_disk_to_db(cfg(), new_run())
    assert row(file_id).state == FileState.DONE.value
    assert file_id not in todo


def test_an_unchanged_candidate_comes_back_as_candidate(tmp_path, marker_done):
    film = video(tmp_path / "lib", "Film.mkv")
    lib_id = add_library(tmp_path / "lib")
    convert = add_row(film, lib_id, state=FileState.MISSING.value, plan={"crf": 30},
                      analyzed_at=dt.datetime(2026, 9, 1),
                      decision_reason="Spart voraussichtlich 2.0 GiB (40%): 5 GiB -> 3 GiB.")
    other = video(tmp_path / "lib", "Other.mkv")
    skip = add_row(other, lib_id, state=FileState.MISSING.value, plan={"crf": 30},
                   analyzed_at=dt.datetime(2026, 9, 1),
                   decision_reason="Nur 5% Ersparnis erwartet (1 GiB) - unter der Schwelle.")
    _, _, todo = scanner._sync_disk_to_db(cfg(), new_run())
    assert row(convert).state == FileState.CANDIDATE.value
    assert row(skip).state == FileState.SKIPPED.value
    assert convert not in todo and skip not in todo


def test_a_changed_file_that_comes_back_is_new(tmp_path, marker_done):
    film = video(tmp_path / "lib", "Film.mkv")
    lib_id = add_library(tmp_path / "lib")
    file_id = add_row(film, lib_id, video_codec="av1", state=FileState.MISSING.value,
                      converted_at=dt.datetime(2026, 1, 1), size=1)
    _, _, todo = scanner._sync_disk_to_db(cfg(), new_run())
    assert row(file_id).state == FileState.NEW.value
    assert file_id in todo


# --------------------------------------------------------------------------- #
# M3: old missing rows are dropped
# --------------------------------------------------------------------------- #

def test_long_missing_rows_are_purged_but_history_stays(tmp_path, marker_done):
    root = tmp_path / "lib"
    video(root, "Stays.mkv")
    lib_id = add_library(root)
    old = dt.datetime.now() - dt.timedelta(days=31)
    purged = add_row(root / "Old.mkv", lib_id, on_disk=False,
                     state=FileState.MISSING.value, last_seen=old)
    busy = add_row(root / "Busy.mkv", lib_id, on_disk=False,
                   state=FileState.MISSING.value, last_seen=old)
    recent = add_row(root / "Recent.mkv", lib_id, on_disk=False,
                     state=FileState.MISSING.value, last_seen=dt.datetime.now())
    with db.session_scope() as s:
        done_job = Job(file_id=purged, state=JobState.DONE.value)
        s.add_all([done_job, Job(file_id=busy, state=JobState.QUEUED.value)])
        s.flush()
        s.add(LearningSample(job_id=done_job.id, features={}))
        s.add(HistoryEntry(message="konvertiert", file_id=purged))

    scanner._sync_disk_to_db(cfg(), new_run())
    assert row(purged) is None
    assert row(busy) is not None and row(recent) is not None
    with db.session_scope() as s:
        entry = s.query(HistoryEntry).filter(HistoryEntry.message == "konvertiert").one()
        assert entry.file_id is None
        assert s.query(LearningSample).one().job_id is None


# --------------------------------------------------------------------------- #
# N1: ignored stays ignored
# --------------------------------------------------------------------------- #

def test_a_changed_ignored_file_stays_ignored(tmp_path, marker_done):
    film = video(tmp_path / "lib", "Film.mkv")
    lib_id = add_library(tmp_path / "lib")
    file_id = add_row(film, lib_id, state=FileState.IGNORED.value, ignored=True, size=1)
    stuck = video(tmp_path / "lib", "Stuck.mkv")
    stuck_id = add_row(stuck, lib_id, state=FileState.NEW.value, ignored=True)
    _, _, todo = scanner._sync_disk_to_db(cfg(), new_run())
    assert row(file_id).state == FileState.IGNORED.value
    assert row(file_id).size == film.stat().st_size
    # Left on "new" by the old code - repaired.
    assert row(stuck_id).state == FileState.IGNORED.value
    assert file_id not in todo and stuck_id not in todo


# --------------------------------------------------------------------------- #
# M5: names that are not UTF-8
# --------------------------------------------------------------------------- #

def test_a_name_that_is_not_utf8_is_skipped_not_fatal(tmp_path, marker_done, monkeypatch):
    root = tmp_path / "lib"
    good = video(root, "Good.mkv")
    lib_id = add_library(root)
    bad = str(root / "Bad\udcff.mkv")   # what os.walk yields for byte 0xff
    real_stat = os.stat

    def walk(top, onerror=None, followlinks=False):
        yield str(root), [], ["Good.mkv", "Bad\udcff.mkv"]

    def stat(path, *a, **kw):
        return real_stat(good if path == bad else path, *a, **kw)

    monkeypatch.setattr(scanner.os, "walk", walk)
    monkeypatch.setattr(scanner.os, "stat", stat)
    seen, new, _ = scanner._sync_disk_to_db(cfg(), new_run())
    assert (seen, new) == (1, 1)
    with db.session_scope() as s:
        assert [r.path for r in s.query(MediaFile).all()] == [str(good)]
        assert s.query(HistoryEntry).filter(HistoryEntry.message.contains("UTF-8")).count() == 1
    assert lib_id


# --------------------------------------------------------------------------- #
# N10 / trash: our own folders are never media
# --------------------------------------------------------------------------- #

def test_own_folders_are_never_scanned(tmp_path):
    root = tmp_path / "lib"
    video(root, "Film.mkv")
    video(root / ".optimizarr-trash" / "2026-09-01", "Old.mkv")
    video(root, ".optimizarr-staging-1-ab-Film.mkv")
    video(root / "Papierkorb", "Trashed.mkv")
    settings = cfg()
    settings.output.trash_dir = str(root / "Papierkorb")
    found = [Path(p).name for _, p, _, _ in scanner.walk_paths([(1, str(root))], settings)]
    assert found == ["Film.mkv"]


# --------------------------------------------------------------------------- #
# M10: no write transaction while the disk is walked
# --------------------------------------------------------------------------- #

def test_the_walk_holds_no_write_lock(tmp_path, monkeypatch, marker_done):
    db_file = tmp_path / "walk.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 0.2})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    root = tmp_path / "lib"
    a = video(root, "A.mkv")
    b = video(root, "B.mkv")
    lib_id = add_library(root)
    run_id = new_run()
    blocked: list[str] = []

    def slow_walk(roots, settings, report=None):
        yield lib_id, str(a), 2048, a.stat().st_mtime
        # Meanwhile the encoder wants to write - on a slow share this is
        # minutes into the walk.
        con = sqlite3.connect(db_file, timeout=0.2)
        try:
            con.execute("INSERT INTO history (level, category, message, created_at) VALUES ('info','x','y','2026-09-26 00:00:00')")
            con.commit()
        except sqlite3.OperationalError as exc:
            blocked.append(str(exc))
        finally:
            con.close()
        yield lib_id, str(b), 2048, b.stat().st_mtime

    monkeypatch.setattr(scanner, "walk_paths", slow_walk)
    seen, new, _ = scanner._sync_disk_to_db(cfg(), run_id)
    engine.dispose()
    assert not blocked
    assert (seen, new) == (2, 2)


# --------------------------------------------------------------------------- #
# Dolby Vision: files probed before the detection worked
# --------------------------------------------------------------------------- #

def test_dv_reprobe_covers_unfinished_files_only():
    def add(name, **kw):
        values = dict(video_codec="hevc", state=FileState.CANDIDATE.value)
        values.update(kw)
        with db.session_scope() as s:
            r = MediaFile(path=f"/media/{name}", **values)
            s.add(r)
            s.flush()
            return r.id

    cand = add("Cand.mkv", hdr_format="hdr10")
    skipped = add("Skip.mkv", video_codec="h264", state=FileState.SKIPPED.value)
    probed = add("Probed.mkv", video_codec="av1", state=FileState.PROBED.value)
    add("Done.mkv", state=FileState.DONE.value)
    add("Queued.mkv", state=FileState.QUEUED.value)
    add("Old.mkv", video_codec="mpeg2video")
    add("Dv.mkv", hdr_format="dolby_vision_p8")
    add("Ignored.mkv", ignored=True)
    assert sorted(scanner._dv_reprobe_ids()) == sorted([cand, skipped, probed])


def fake_hw(monkeypatch):
    monkeypatch.setattr(hwaccel, "cached", lambda: None)
    monkeypatch.setattr(hwaccel, "detect", AsyncMock(return_value=None))


def test_the_rescan_skips_dv_and_leaves_everything_else_alone(tmp_path, monkeypatch):
    fake_hw(monkeypatch)
    root = tmp_path / "lib"
    lib_id = add_library(root)
    dv = add_row(video(root, "Dv.mkv"), lib_id, hdr_format="hdr10", plan={"crf": 30},
                 decision_reason="Spart voraussichtlich viel")
    plain = add_row(video(root, "Plain.mkv"), lib_id, state=FileState.SKIPPED.value,
                    hdr_format="hdr10", decision_reason="Nur 5% Ersparnis erwartet")
    analysed = []

    async def probe(path, timeout=120.0):
        fmt = "dolby_vision_p5" if path.endswith("Dv.mkv") else "hdr10"
        return MediaInfo(path=path, size=8 * 1024**3, duration=7200, video_codec="hevc",
                         width=3840, height=2160, fps=24, video_bitrate=40_000_000,
                         bit_depth=10, is_hdr=True, hdr_format=fmt)

    real_analyze = analyzer.analyze

    async def analyze(info, *a, **kw):
        analysed.append(Path(info.path).name)
        return await real_analyze(info, *a, **kw)

    monkeypatch.setattr(ffmpeg, "probe", probe)
    monkeypatch.setattr(analyzer, "analyze", analyze)
    config.update_settings({"library": {"min_file_size_mb": 0}})
    result = asyncio.run(scanner.run_scan(trigger="test"))
    assert result["ok"], result
    stored = row(dv)
    assert stored.state == FileState.SKIPPED.value
    assert stored.hdr_format == "dolby_vision_p5"
    assert "Dolby Vision" in stored.decision_reason
    # Not DV: verdict and state untouched, and no (trial-encode) analysis.
    assert row(plain).state == FileState.SKIPPED.value
    assert row(plain).decision_reason == "Nur 5% Ersparnis erwartet"
    assert analysed == ["Dv.mkv"]
    assert scanner._dv_marker().exists()

    # Only once.
    probes = []
    monkeypatch.setattr(ffmpeg, "probe", AsyncMock(side_effect=lambda p, **k: probes.append(p)))
    asyncio.run(scanner.run_scan(trigger="test"))
    assert probes == []


# --------------------------------------------------------------------------- #
# M4: the scan lock is always released
# --------------------------------------------------------------------------- #

def test_a_failing_hardware_probe_releases_the_scan_lock(monkeypatch):
    monkeypatch.setattr(hwaccel, "cached", lambda: None)
    monkeypatch.setattr(hwaccel, "detect", AsyncMock(side_effect=RuntimeError("kaputt")))
    result = asyncio.run(scanner.run_scan(trigger="test"))
    assert not result["ok"] and "kaputt" in result["error"]
    assert scanner.state.running is False
    with db.session_scope() as s:
        run = s.get(ScanRun, result["run_id"])
        assert run.state == "failed" and "kaputt" in run.error


def test_failing_settings_release_the_scan_lock(monkeypatch):
    def broken(force=False):
        raise RuntimeError("settings kaputt")

    monkeypatch.setattr(scanner, "load_settings", broken)
    result = asyncio.run(scanner.run_scan(trigger="test"))
    assert not result["ok"]
    assert scanner.state.running is False


# --------------------------------------------------------------------------- #
# M6: cancel means cancel
# --------------------------------------------------------------------------- #

def test_gather_stops_on_cancel(monkeypatch):
    monkeypatch.setattr(scanner, "CANCEL_GRACE", 0.05, raising=False)

    async def main():
        cancel = asyncio.Event()
        finished = []

        async def forever():
            await asyncio.sleep(3600)
            finished.append(1)

        asyncio.get_running_loop().call_later(0.01, cancel.set)
        await asyncio.wait_for(scanner._gather_limited([forever(), forever()], cancel), 2)
        return finished

    assert asyncio.run(main()) == []


def test_cooperative_work_gets_a_grace_period(monkeypatch):
    monkeypatch.setattr(scanner, "CANCEL_GRACE", 2.0, raising=False)

    async def main():
        cancel = asyncio.Event()
        cleaned = []

        async def polite():
            await cancel.wait()
            await asyncio.sleep(0.05)       # e.g. ffmpeg being terminated
            cleaned.append(1)

        asyncio.get_running_loop().call_later(0.01, cancel.set)
        await asyncio.wait_for(scanner._gather_limited([polite()], cancel), 2)
        return cleaned

    assert asyncio.run(main()) == [1]


def test_an_analysis_finished_after_cancel_is_not_stored(tmp_path, monkeypatch):
    fake_hw(monkeypatch)
    film = video(tmp_path / "lib", "Film.mkv")
    file_id = add_row(film, None, state=FileState.PROBED.value)

    async def probe(path, timeout=120.0):
        return MediaInfo(path=path, size=8 * 1024**3, duration=7200, video_codec="h264",
                         width=1920, height=1080, fps=24, video_bitrate=20_000_000)

    async def analyze(info, *a, **kw):
        scanner.cancel_scan()           # the user hits "Abbrechen" meanwhile
        return analyzer.AnalysisResult(decision="convert", reason="Spart voraussichtlich viel")

    monkeypatch.setattr(ffmpeg, "probe", probe)
    monkeypatch.setattr(analyzer, "analyze", analyze)
    result = asyncio.run(scanner.run_scan(trigger="test", analyze_only_ids=[file_id]))
    assert result["error"] == scanner.CANCELLED_MESSAGE
    assert row(file_id).state == FileState.PROBED.value
    assert row(file_id).decision_reason == ""
    with db.session_scope() as s:
        assert s.get(ScanRun, result["run_id"]).state == "cancelled"


# --------------------------------------------------------------------------- #
# Codec exclusion lifted -> analysis starts right away
# --------------------------------------------------------------------------- #

def _excluded_hevc() -> int:
    with db.session_scope() as s:
        row_ = MediaFile(path="/media/x.mkv", video_codec="hevc", state=FileState.SKIPPED.value,
                         decision_reason=f"HEVC / H.265 {codecs.EXCLUSION_REASON}")
        s.add(row_)
        s.flush()
        return row_.id


def test_lifting_an_exclusion_starts_an_analysis_from_the_threadpool(monkeypatch):
    file_id = _excluded_hevc()
    calls = []

    async def fake_run_scan(trigger="manual", analyze_only_ids=None, depth=None):
        calls.append((trigger, analyze_only_ids))
        return {"ok": True}

    monkeypatch.setattr(scanner, "run_scan", fake_run_scan)

    async def main():
        monkeypatch.setattr(bus, "_loop", asyncio.get_running_loop())
        # put_settings is a plain def - FastAPI runs it in a worker thread.
        result = await asyncio.to_thread(scanner.apply_codec_exclusions, ["av1", "hevc"], ["av1"])
        for _ in range(5):
            await asyncio.sleep(0)
        return result

    result = asyncio.run(main())
    assert result["restored"] == 1 and result["analysis_started"] is True
    assert calls == [(scanner.SETTINGS_TRIGGER, [file_id])]


def test_a_running_scan_takes_the_request_over_when_it_ends(monkeypatch):
    file_id = _excluded_hevc()
    calls = []

    async def fake_run_scan(trigger="manual", analyze_only_ids=None, depth=None):
        calls.append(analyze_only_ids)
        return {"ok": True}

    monkeypatch.setattr(scanner, "run_scan", fake_run_scan)

    async def main():
        monkeypatch.setattr(bus, "_loop", asyncio.get_running_loop())
        scanner.state.running = True
        await asyncio.to_thread(scanner.apply_codec_exclusions, ["av1", "hevc"], ["av1"])
        await asyncio.sleep(0.01)
        assert calls == []                      # not while the other scan runs
        scanner.state.running = False
        scanner._start_pending_analysis()       # what run_scan's finally schedules
        await asyncio.sleep(0.01)

    asyncio.run(main())
    assert calls == [[file_id]]


def test_without_an_event_loop_the_next_scan_picks_it_up(monkeypatch):
    _excluded_hevc()
    monkeypatch.setattr(bus, "_loop", None)
    result = scanner.apply_codec_exclusions(["av1", "hevc"], ["av1"])
    assert result["restored"] == 1 and result["analysis_started"] is False
