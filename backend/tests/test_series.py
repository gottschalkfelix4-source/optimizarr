"""Series overview: grouping by folder, progress per series, forcing excluded files."""
import asyncio
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(Path(tempfile.gettempdir()) / "optimizarr-pytest/config"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db  # noqa: E402
from app.config import AppSettings  # noqa: E402
from app.core import encoder, planner, series, worker  # noqa: E402
from app.core.ffmpeg import MediaInfo  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Job, LibraryPath, MediaFile  # noqa: E402

ROOT = "/media/tv"


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(config, "_cache", None)
    yield
    engine.dispose()


@pytest.fixture()
def client():
    # Not a context manager on purpose: that would run the lifespan and start
    # the queue worker, which would go after the jobs these tests create.
    return TestClient(app)


@pytest.fixture()
def library():
    with db.session_scope() as s:
        lib = LibraryPath(path=ROOT, name="Serien", enabled=True)
        s.add(lib)
        s.flush()
        return lib.id


def _add(lib_id, rel, *, codec="h264", state="candidate", plan=True, **kw) -> int:
    with db.session_scope() as s:
        row = MediaFile(
            path=f"{ROOT}/{rel}", library_id=lib_id, video_codec=codec, state=state,
            size=kw.pop("size", 2 * 1024**3),
            estimated_saving_bytes=kw.pop(
                "estimated_saving_bytes", 1024**3 if state == "candidate" else 0
            ),
            plan={"encoder": "libsvtav1", "crf": 30} if plan else None,
            **kw,
        )
        s.add(row)
        s.flush()
        return row.id


def _file(file_id) -> MediaFile:
    with db.session_scope() as s:
        return s.get(MediaFile, file_id)


def _job_for(file_id) -> Job:
    with db.session_scope() as s:
        return s.query(Job).filter(Job.file_id == file_id).order_by(Job.id.desc()).first()


# --------------------------------------------------------------------------- #
# Reading paths
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rel,expected", [
    ("Breaking Bad (2008)/Season 01/Breaking Bad - S01E03 - Title.mkv", ("Breaking Bad (2008)", 1, 3)),
    ("Dark/Staffel 2/Dark.s02e07.German.1080p.mkv", ("Dark", 2, 7)),
    ("Dark/Staffel 2/Folge 7.mkv", ("Dark", 2, None)),
    ("Doctor Who/Specials/Doctor Who - Christmas.mkv", ("Doctor Who", 0, None)),
    ("Futurama/Futurama 1x02 Pilot.avi", ("Futurama", 1, 2)),
    ("Show.Name.S03E10.720p.mkv", ("Show Name", 3, 10)),
    # A resolution in the name is not "season 20, episode 108".
    ("Movie (2019)/Movie.1920x1080.mkv", ("Movie (2019)", None, None)),
])
def test_place_reads_sonarr_style_paths(rel, expected):
    spot = series.place(f"{ROOT}/{rel}", ROOT)
    assert (spot.series, spot.season, spot.episode) == expected


def test_files_outside_the_root_or_loose_movies_are_not_placed():
    assert series.place("/media/tv-old/Show/Show S01E01.mkv", ROOT) is None
    assert series.place(f"{ROOT}/Some Movie (2020).mkv", ROOT) is None


def test_windows_separators_are_understood():
    spot = series.place(r"D:\TV\Show\Season 1\Show S01E02.mkv", "D:\\TV")
    assert (spot.series, spot.season, spot.episode) == ("Show", 1, 2)


@pytest.mark.parametrize("state,codec,ignored,expected", [
    ("done", "av1", False, "converted"),
    ("done", "h264", False, "converted"),     # sidecar mode keeps the source codec
    ("skipped", "av1", False, "av1"),
    ("skipped", "av01", False, "av1"),
    ("queued", "av1", False, "active"),       # forced re-encode of an AV1 file
    ("candidate", "hevc", False, "pending"),
    ("candidate", "hevc", True, "excluded"),
    ("skipped", "hevc", False, "excluded"),
    ("failed", "h264", False, "failed"),
    ("probed", "h264", False, "other"),
])
def test_bucket(state, codec, ignored, expected):
    assert series.bucket(state, codec, ignored) == expected


def test_markers_alone_are_not_a_plan():
    assert planner.EncodePlan.from_dict({planner.FORCED: True}) is None
    assert planner.plan_fields({planner.FORCED: True, "crf": 28}) == {"crf": 28}
    assert planner.plan_fields({planner.FORCED: True}) is None
    assert planner.job_markers({"crf": 28, planner.FORCED: True}) == {planner.FORCED: True}


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

def test_overview_counts_progress_per_series(client, library):
    _add(library, "Dark/Season 01/Dark - S01E01.mkv", codec="av1", state="done",
         original_size=3 * 1024**3, size=1024**3, converted_at=dt.datetime(2026, 9, 1, 12, 0))
    _add(library, "Dark/Season 01/Dark - S01E02.mkv", codec="av1", state="skipped", plan=False)
    _add(library, "Dark/Season 02/Dark - S02E01.mkv", codec="hevc")
    _add(library, "Dark/Season 02/Dark - S02E02.mkv", codec="hevc", state="skipped", plan=False)
    _add(library, "Some Film (2020)/Some Film.mkv")          # a movie, not a series

    body = client.get("/api/series").json()
    assert [s["name"] for s in body["items"]] == ["Dark"]
    dark = body["items"][0]
    assert dark["key"] == f"{library}:Dark"
    assert dark["episodes"] == 4 and dark["season_count"] == 2
    assert dark["in_av1"] == 2
    assert dark["counts"]["converted"] == 1 and dark["counts"]["av1"] == 1
    assert dark["counts"]["pending"] == 1 and dark["counts"]["excluded"] == 1
    assert dark["saved_bytes"] == 2 * 1024**3
    assert dark["potential_saving"] == 1024**3
    assert dark["last_converted"].startswith("2026-09-01")
    assert body["totals"]["episodes"] == 4


def test_detail_lists_seasons_in_order_with_episode_numbers(client, library):
    _add(library, "Dark/Specials/Dark - Making of.mkv", state="skipped", plan=False)
    _add(library, "Dark/Season 02/Dark - S02E01.mkv")
    _add(library, "Dark/Season 01/Dark - S01E02.mkv")
    _add(library, "Dark/Season 01/Dark - S01E01.mkv")

    body = client.get("/api/series/detail", params={"key": f"{library}:Dark"}).json()
    assert [s["label"] for s in body["seasons"]] == ["Staffel 1", "Staffel 2", "Specials"]
    first = body["seasons"][0]
    assert first["episodes"] == 2            # the count survives next to the list
    assert [e["episode"] for e in first["files"]] == [1, 2]
    assert first["files"][0]["bucket"] == "pending"
    assert first["files"][0]["name"] == "Dark - S01E01.mkv"


def test_unknown_series_is_404(client, library):
    assert client.get("/api/series/detail", params={"key": f"{library}:Nope"}).status_code == 404
    assert client.get("/api/series/detail", params={"key": "garbage"}).status_code == 404


# --------------------------------------------------------------------------- #
# Queueing
# --------------------------------------------------------------------------- #

def test_series_enqueue_without_force_takes_only_candidates(client, library):
    candidate = _add(library, "Dark/Season 01/Dark - S01E01.mkv")
    excluded = _add(library, "Dark/Season 01/Dark - S01E02.mkv", codec="hevc",
                    state="skipped", plan=False)

    r = client.post("/api/series/enqueue", json={"key": f"{library}:Dark"}).json()
    assert r["added"] == 1
    assert _file(candidate).state == "queued"
    assert _file(excluded).state == "skipped"
    assert not planner.is_forced(_job_for(candidate).plan)


def test_forcing_a_season_queues_excluded_files_but_leaves_av1_alone(client, library):
    excluded = _add(library, "Dark/Season 01/Dark - S01E01.mkv", codec="hevc",
                    state="skipped", plan=False)
    ignored = _add(library, "Dark/Season 01/Dark - S01E02.mkv", state="ignored", ignored=True)
    av1 = _add(library, "Dark/Season 01/Dark - S01E03.mkv", codec="av1", state="skipped", plan=False)
    other_season = _add(library, "Dark/Season 02/Dark - S02E01.mkv", codec="hevc",
                        state="skipped", plan=False)

    r = client.post("/api/series/enqueue",
                    json={"key": f"{library}:Dark", "season": 1, "force": True}).json()
    assert r["added"] == 2
    assert _file(excluded).state == "queued"
    assert _file(ignored).state == "queued"
    assert _file(av1).state == "skipped"
    assert _file(other_season).state == "skipped"

    job = _job_for(excluded)
    assert planner.is_forced(job.plan)
    assert job.plan[planner.RESTORE_STATE] == "skipped"
    # Excluded before a plan existed: the encoder builds one when the job starts.
    assert planner.EncodePlan.from_dict(job.plan) is None
    # A plan the analysis did build is carried over.
    assert planner.EncodePlan.from_dict(_job_for(ignored).plan).encoder == "libsvtav1"


def test_cancelling_a_forced_job_puts_the_file_back(client, library):
    excluded = _add(library, "Dark/Season 01/Dark - S01E01.mkv", codec="hevc",
                    state="skipped", plan=False)
    assert client.post("/api/jobs", json={"file_ids": [excluded]}).json()["added"] == 0

    assert client.post("/api/jobs", json={"file_ids": [excluded], "force": True}).json()["added"] == 1
    job = _job_for(excluded)
    shown = client.get(f"/api/jobs/{job.id}").json()
    assert shown["forced"] is True
    assert shown["plan"] is None           # markers are not a plan to display

    assert client.post(f"/api/jobs/{job.id}/cancel").status_code == 200
    # "candidate" would have auto-queue pick up a file nobody planned.
    assert _file(excluded).state == "skipped"

    assert client.post(f"/api/jobs/{job.id}/retry").status_code == 200
    assert planner.is_forced(_job_for(excluded).plan)


def test_missing_files_are_not_queued_even_when_forced(library):
    gone = _add(library, "Dark/Season 01/Dark - S01E09.mkv", state="missing")
    added, skipped = worker.enqueue_files([gone], force=True)
    assert added == 0
    assert "fehlt" in skipped[0]


# --------------------------------------------------------------------------- #
# Encoding a forced job
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("forced,output_size,accepted", [
    (True, 95, True),       # 5% is below the threshold - accepted because forced
    (False, 95, False),     # the same result, not forced: rejected as before
    (True, 150, False),     # forced or not, a bigger file never replaces the original
])
def test_forced_job_skips_the_saving_threshold_but_not_the_size_gate(
    monkeypatch, tmp_path, forced, output_size, accepted,
):
    source = tmp_path / "Dark - S01E01.mkv"
    source.write_bytes(b"x" * 100)
    with db.session_scope() as s:
        media = MediaFile(path=str(source), video_codec="hevc", state="queued")
        s.add(media)
        s.flush()
        plan = (
            {planner.FORCED: True, planner.RESTORE_STATE: "skipped"}
            if forced else planner.EncodePlan().to_dict()
        )
        job = Job(file_id=media.id, plan=plan)
        s.add(job)
        s.flush()
        job_id = job.id

    cfg = AppSettings()
    cfg.analysis.use_learning_model = False
    cfg.queue.min_free_disk_gb = 0
    cfg.output.verify_vmaf = False
    cfg.output.require_smaller = True
    cfg.output.min_accept_saving_percent = 20
    monkeypatch.setattr(encoder, "TRANSCODE_DIR", tmp_path)
    probe = MediaInfo(path=str(source), size=100, duration=1400, width=1920, height=1080,
                      fps=24, video_codec="hevc", video_bitrate=4_000_000)
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=probe))

    used: list[planner.EncodePlan] = []

    async def fake_encode(plan, source_info, dest, *args):
        used.append(plan)
        Path(dest).write_bytes(b"y" * output_size)
        return 0, ""

    monkeypatch.setattr(encoder, "_run_encode", fake_encode)
    monkeypatch.setattr(encoder.quality, "verify_output", AsyncMock(return_value=(True, "")))
    commit = Mock(return_value=str(tmp_path / "result.mkv"))
    monkeypatch.setattr(encoder, "_commit_output", commit)
    monkeypatch.setattr(encoder, "_record_success", Mock())

    outcome = asyncio.run(encoder.run_job(job_id, cfg, None, asyncio.Event()))
    assert outcome.ok is accepted
    assert bool(commit.call_count) is accepted
    assert used and used[0].encoder

    if forced:
        # The plan built at start is kept on the job, markers included.
        with db.session_scope() as s:
            stored = s.get(Job, job_id).plan
        assert stored["encoder"] == used[0].encoder
        assert planner.is_forced(stored)
