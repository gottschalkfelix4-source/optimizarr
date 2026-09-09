"""H.264 migration must reach the queue and survive the output size gates."""
import asyncio
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

from app import config, db
from app.config import AppSettings
from app.core import analyzer, encoder, planner, quality, scanner
from app.core.advisor import Advice
from app.core.ffmpeg import MediaInfo
from app.models import Base, Job, LibraryPath, MediaFile, ScanRun


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


def settings(enabled=True):
    cfg = AppSettings()
    cfg.analysis.convert_all_h264 = enabled
    cfg.analysis.use_learning_model = False
    return cfg


@pytest.mark.parametrize("codec", ["h264", "H.264", "x264", "AVC1"])
def test_lean_h264_and_aliases_are_candidates(codec):
    result = asyncio.run(analyzer.analyze(info(video_codec=codec), settings(), None, depth="quick"))
    assert result.should_convert and result.plan is not None
    assert "unabhaengig" in result.reason


def test_normal_mode_and_other_codecs_still_skip_lean_sources():
    assert analyzer.precheck(info(), settings(False))[0]
    assert analyzer.precheck(info(video_codec="hevc"), settings())[0]
    assert analyzer.precheck(info(video_codec="av1"), settings())[0]


def test_small_short_h264_is_allowed_but_invalid_dimensions_and_exclusions_are_not():
    assert not analyzer.precheck(info(size=100, duration=10), settings())[0]
    assert analyzer.precheck(info(width=0), settings())[0]
    cfg = settings()
    cfg.analysis.skip_codecs.append("x264")
    assert analyzer.precheck(info(), cfg)[0]


def test_negative_prediction_and_advisor_veto_do_not_prevent_migration():
    result = analyzer.AnalysisResult(estimated_size=2 * 1024**3,
        estimated_saving_bytes=-1024**3, estimated_saving_pct=-100, confidence=0.1)
    analyzer._decide(result, info(), settings(), Advice(ok=True, recommend_convert=False))
    assert result.should_convert


def test_poor_quick_estimate_does_not_cancel_requested_samples(monkeypatch, tmp_path):
    samples = AsyncMock(return_value=analyzer.SampleResult(ok=True, measured_bitrate=2_000_000, segments=1))
    monkeypatch.setattr(analyzer, "run_samples", samples)
    result = asyncio.run(analyzer.analyze(info(), settings(), None, depth="sample", workroot=tmp_path))
    samples.assert_awaited_once()
    assert result.depth == "sample" and result.should_convert


def test_setting_change_reanalyses_unchanged_files_on_next_scan(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    video = tmp_path / "film.mkv"
    video.write_bytes(b"small video")
    with db.session_scope() as s:
        lib = LibraryPath(path=str(tmp_path), enabled=True)
        run = ScanRun(trigger="test")
        s.add_all([lib, run])
        s.flush()
        row = MediaFile(path=str(video), library_id=lib.id, size=video.stat().st_size,
                        mtime=video.stat().st_mtime, video_codec="h264", state="skipped")
        s.add(row)
        s.flush()
        file_id, run_id = row.id, run.id
    client = TestClient(app)
    response = client.put("/api/settings", json={"analysis": {"convert_all_h264": True}})
    assert response.status_code == 200
    assert response.json()["applied"]["h264_reanalysis"] == 1
    cfg = config.load_settings()
    assert cfg.library.rescan_changed_only
    _, _, todo = scanner._sync_disk_to_db(cfg, run_id)
    assert file_id in todo
    # Small files were previously filtered before their codec was even known.
    assert list(scanner.walk_paths([(1, str(tmp_path))], cfg))
    assert not list(scanner.walk_paths([(1, str(tmp_path))], settings(False)))


@pytest.mark.parametrize("before,after", [(False, True), (True, False)])
def test_toggle_only_invalidates_inactive_h264_decisions(before, after):
    states = ["candidate", "skipped", "queued", "encoding", "done", "failed"]
    with db.session_scope() as s:
        for state in states:
            s.add(MediaFile(path=f"/{state}.mkv", video_codec="H.264", state=state,
                            plan={"encoder": "libsvtav1"}, estimated_saving_bytes=123))
        s.add(MediaFile(path="/ignored.mkv", video_codec="h264", state="skipped", ignored=True))
        s.add(MediaFile(path="/hevc.mkv", video_codec="hevc", state="candidate"))
    assert scanner.apply_h264_conversion_change(before, after) == 2
    with db.session_scope() as s:
        rows = {r.path: r for r in s.query(MediaFile).all()}
        for state in states:
            row = rows[f"/{state}.mkv"]
            assert row.state == ("probed" if state in ("candidate", "skipped") else state)
        assert rows["/candidate.mkv"].plan is None
        assert rows["/candidate.mkv"].estimated_saving_bytes == 0
        assert rows["/ignored.mkv"].state == "skipped"
        assert rows["/hevc.mkv"].state == "candidate"


def test_auto_queue_accepts_negative_h264_savings_but_not_low_hevc_savings():
    with db.session_scope() as s:
        for codec, pct, ignored in [("AVC1", -100, False), ("hevc", 1, False),
                                    ("hevc", 40, False), ("h264", 0, True)]:
            s.add(MediaFile(path=f"/{codec}-{pct}.mkv", video_codec=codec, state="candidate",
                            estimated_saving_pct=pct, plan=planner.EncodePlan().to_dict(), ignored=ignored))
    assert scanner._auto_queue(settings()) == 2
    assert scanner._auto_queue(settings()) == 0
    with db.session_scope() as s:
        assert s.query(Job).count() == 2


@pytest.mark.parametrize("enabled,codec,gate,accepted", [
    (True, "h264", "ok", True), (True, "avc1", "ok", True),
    (False, "h264", "ok", False), (True, "hevc", "ok", False),
    (True, "h264", "integrity", False), (True, "h264", "quality", False),
])
def test_job_accepts_larger_h264_only_in_migration_mode(monkeypatch, tmp_path, enabled, codec, gate, accepted):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x" * 100)
    with db.session_scope() as s:
        media = MediaFile(path=str(source), video_codec=codec, state="queued")
        s.add(media)
        s.flush()
        job = Job(file_id=media.id, plan=planner.EncodePlan().to_dict())
        s.add(job)
        s.flush()
        job_id = job.id
    cfg = settings(enabled)
    cfg.queue.min_free_disk_gb = 0
    cfg.output.verify_vmaf = True
    monkeypatch.setattr(encoder, "TRANSCODE_DIR", tmp_path)
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=info(path=str(source), size=100, video_codec=codec)))

    async def fake_encode(plan, source_info, dest, *args):
        Path(dest).write_bytes(b"y" * 150)
        return 0, ""

    monkeypatch.setattr(encoder, "_run_encode", fake_encode)
    verify = AsyncMock(return_value=(gate != "integrity", "invalid streams"))
    monkeypatch.setattr(encoder.quality, "verify_output", verify)
    score = 80 if gate == "quality" else 96
    monkeypatch.setattr(encoder, "_spot_check_quality", AsyncMock(return_value=quality.QualityScore(score, "vmaf", score)))
    commit = Mock(return_value=str(tmp_path / "result.mkv"))
    monkeypatch.setattr(encoder, "_commit_output", commit)
    monkeypatch.setattr(encoder, "_record_success", Mock())
    outcome = asyncio.run(encoder.run_job(job_id, cfg, None, asyncio.Event()))
    assert outcome.ok is accepted
    if accepted:
        assert "50% groesser" in outcome.reason
    assert bool(commit.call_count) is accepted
    verify.assert_awaited_once()
    assert source.read_bytes() == b"x" * 100
