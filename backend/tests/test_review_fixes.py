"""Regression tests for the findings of the September 2026 code review."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(Path(tempfile.gettempdir()) / "optimizarr-pytest/config"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db
from app.config import AppSettings
from app.core import planner, quality, scanner, series, worker
from app.core.ffmpeg import MediaInfo
from app.models import Base, FileState, Job, JobState, MediaFile


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(config, "_cache", None)
    yield
    engine.dispose()


def info(**kwargs):
    values = dict(path="/media/film.mkv", size=1024**3, duration=7200,
                  width=1920, height=1080, fps=24, video_codec="h264", video_bitrate=100_000)
    values.update(kwargs)
    return MediaInfo(**values)


def sub(index, codec, language="ger"):
    return {"index": index, "codec": codec, "language": language}


# --- subtitles the target container cannot store -------------------------- #

def test_mkv_converts_mov_text_and_drops_teletext():
    source = info(subtitle_streams=[sub(2, "mov_text"), sub(3, "dvb_teletext"), sub(4, "ass")])
    actions = {a.index: a for a in planner.plan_subtitles(source, AppSettings(), "mkv")}
    assert (actions[2].action, actions[2].target) == ("convert", "srt")
    assert actions[3].action == "drop"
    assert actions[4].action == "copy"


def test_mp4_turns_text_subtitles_into_mov_text():
    source = info(subtitle_streams=[sub(2, "subrip"), sub(3, "hdmv_pgs_subtitle"), sub(4, "mov_text")])
    actions = {a.index: a for a in planner.plan_subtitles(source, AppSettings(), "mp4")}
    assert (actions[2].action, actions[2].target) == ("convert", "mov_text")
    assert actions[3].action == "drop"
    assert actions[4].action == "copy"


def test_subtitle_codecs_are_set_per_output_stream():
    plan = planner.EncodePlan(subtitles=[
        {"index": 2, "action": "convert", "codec": "mov_text", "language": "ger", "target": "srt"},
        {"index": 3, "action": "drop", "codec": "dvb_teletext", "language": "ger"},
        {"index": 4, "action": "copy", "codec": "ass", "language": "ger"},
    ])
    args = planner.build_ffmpeg_args(plan, info(), "/media/film.mkv", "/tmp/out.mkv")
    joined = " ".join(args)
    assert "-c:s:0 srt" in joined and "-c:s:1 copy" in joined
    assert "-c:s copy" not in joined


# --- never a silent result ------------------------------------------------ #

def test_language_rules_never_drop_every_audio_track():
    cfg = AppSettings()
    cfg.audio.keep_languages = ["deu"]
    cfg.audio.keep_default_track_always = False
    source = info(audio_streams=[
        {"index": 1, "codec": "ac3", "channels": 6, "language": "eng", "bitrate": 448_000},
        {"index": 2, "codec": "ac3", "channels": 2, "language": "fra", "default": True},
    ])
    actions, _ = planner.plan_audio(source, cfg)
    kept = [a for a in actions if a.action != "drop"]
    assert [a.index for a in kept] == [2]


def test_output_without_audio_is_rejected(monkeypatch, tmp_path):
    out = tmp_path / "out.mkv"
    out.write_bytes(b"x" * 4096)
    monkeypatch.setattr(quality.ffmpeg, "probe", AsyncMock(return_value=info(
        video_codec="av1", audio_streams=[])))
    ok, reason = asyncio.run(quality.verify_output(
        info(audio_streams=[{"index": 1, "codec": "ac3"}]), str(out)))
    assert not ok and "Tonspur" in reason


# --- state bookkeeping ---------------------------------------------------- #

def test_crashed_job_is_closed_out_instead_of_running_forever():
    with db.session_scope() as s:
        media = MediaFile(path="/media/a.mkv", state=FileState.ENCODING.value)
        s.add(media)
        s.flush()
        job = Job(file_id=media.id, state=JobState.RUNNING.value)
        s.add(job)
        s.flush()
        job_id, file_id = job.id, media.id
    worker._fail_crashed_job(job_id, PermissionError("/transcode"))
    with db.session_scope() as s:
        assert s.get(Job, job_id).state == JobState.FAILED.value
        assert s.get(MediaFile, file_id).state == FileState.FAILED.value


@pytest.mark.parametrize("busy", [FileState.QUEUED.value, FileState.ENCODING.value])
def test_reanalysis_does_not_touch_a_busy_file(busy):
    with db.session_scope() as s:
        media = MediaFile(path="/media/a.mkv", state=busy)
        s.add(media)
        s.flush()
        file_id = media.id
    scanner._store_probe(file_id, info())
    with db.session_scope() as s:
        assert s.get(MediaFile, file_id).state == busy


def test_kept_originals_are_not_scanned_again(tmp_path):
    (tmp_path / "Film.mkv").write_bytes(b"x" * 10)
    (tmp_path / "Film.original.mkv").write_bytes(b"x" * 10)
    (tmp_path / ".optimizarr-staging-1-ab-Film.mkv").write_bytes(b"x" * 10)
    cfg = AppSettings()
    cfg.library.min_file_size_mb = 0
    found = [Path(p).name for _, p, _, _ in scanner.walk_paths([(1, str(tmp_path))], cfg)]
    assert found == ["Film.mkv"]


# --- series vs. movies ---------------------------------------------------- #

def _row(path, id_=1):
    return SimpleNamespace(id=id_, path=path, library_id=1, state="candidate", video_codec="h264",
                           ignored=False, size=1, original_size=0, estimated_saving_bytes=0,
                           converted_at=None)


def test_specials_folder_does_not_make_a_movie_a_series():
    rows = [_row("/media/Film (2020)/Film (2020).mkv"),
            _row("/media/Film (2020)/Specials/Making of.mkv", 2)]
    (g,) = series.group(rows, {1: ("/media", "Filme")})
    assert not g.looks_like_series


def test_real_series_still_counts_as_series():
    rows = [_row("/media/Show/Season 01/Show - S01E01.mkv")]
    (g,) = series.group(rows, {1: ("/media", "Serien")})
    assert g.looks_like_series
