"""Quality check follow-ups: frame pairing, cancelling helper processes, old trash."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(Path(tempfile.gettempdir()) / "optimizarr-pytest/config"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db  # noqa: E402
from app.config import AppSettings  # noqa: E402
from app.core import encoder, ffmpeg, quality  # noqa: E402
from app.core.ffmpeg import MediaInfo  # noqa: E402
from app.core import output_files, trash
from app.models import Base, FileState, Job, JobState, MediaFile  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(encoder, "TRANSCODE_DIR", tmp_path / "transcode")
    monkeypatch.setattr(output_files, "COMMIT_JOURNAL_DIR", tmp_path / "config" / "pending-commits")
    monkeypatch.setattr(output_files, "TRASH_ROOTS_FILE", tmp_path / "config" / "trash-roots.json")
    monkeypatch.setattr(encoder, "CONFIG_DIR", tmp_path / "config")
    (tmp_path / "transcode").mkdir()
    yield
    engine.dispose()


def _info(**kw):
    values = dict(path="/lib/Film.mkv", size=1000, duration=1400, width=1920, height=1080,
                  fps=24000 / 1001, video_codec="hevc", video_bitrate=4_000_000)
    values.update(kw)
    return MediaInfo(**values)


# --- 1. frame pairing ------------------------------------------------------------ #

def test_compare_graph_pairs_frames_by_index_on_both_pads():
    graph = quality.build_compare_graph("ssim", 1920, 1080)
    dist, ref, _ = graph.split(";")
    assert "STARTPTS" not in graph
    for pad in (dist, ref):
        assert "settb=AVTB,setpts=N," in pad
    # The index has to be set before the encode is scaled.
    assert dist.index("setpts=N") < dist.index("scale=1920:1080")


def _have_ffmpeg():
    return bool(ffmpeg.FFMPEG) and (shutil.which(ffmpeg.FFMPEG) or os.path.exists(ffmpeg.FFMPEG))


def _clip(path, phase, codec=("-c:v", "ffv1")):
    """Six seconds of 24000/1001 fps video, timestamps shifted by ``phase`` s.

    The shift is below a millisecond, as a 90 kHz Blu-ray clock leaves it;
    Matroska rounds some of those timestamps the other way than an unshifted
    copy of the same pictures.
    """
    subprocess.run([
        ffmpeg.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24000/1001:duration=6",
        "-vf", f"settb=1/90000,setpts=PTS+{phase}/TB", "-fps_mode", "passthrough",
        "-enc_time_base", "1:90000", *codec, str(path),
    ], check=True)


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not installed")
@pytest.mark.parametrize("metric", ["ssim", "vmaf"])
def test_identical_pictures_with_jittered_ms_timestamps_score_as_identical(
    tmp_path, monkeypatch, metric
):
    filters = asyncio.run(ffmpeg.available_filters())
    if metric == "vmaf" and "libvmaf" not in filters:
        pytest.skip("ffmpeg without libvmaf")
    ref, dist = tmp_path / "ref.mkv", tmp_path / "dist.mkv"
    _clip(ref, 0.00031)
    _clip(dist, 0.0)
    monkeypatch.setattr(quality, "available_metric", AsyncMock(return_value=metric))
    score = asyncio.run(quality.measure_quality(
        str(ref), str(dist), threads=2, timeout=120, scale_to_reference=False,
    ))
    assert score is not None and score.metric == metric
    # Paired by timestamp, about one frame in 24 met its predecessor here:
    # SSIM 0.97 / VMAF 78 for two copies of the same pictures.
    if metric == "ssim":
        assert score.value > 0.9999
    else:
        assert score.value > 99.0


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not installed")
def test_an_mp4_encode_is_paired_with_a_matroska_reference(tmp_path, monkeypatch):
    """Different timebases (ms against 90 kHz) must not shift the pairing either."""
    ref, same, dist = tmp_path / "ref.mkv", tmp_path / "same.mkv", tmp_path / "dist.mp4"
    _clip(ref, 0.00031)
    _clip(same, 0.0)
    _clip(dist, 0.0, ("-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p"))
    monkeypatch.setattr(quality, "available_metric", AsyncMock(return_value="ssim"))
    lossless = asyncio.run(quality.measure_quality(str(same), str(dist), scale_to_reference=False))
    jittered = asyncio.run(quality.measure_quality(str(ref), str(dist), scale_to_reference=False))
    assert lossless is not None and jittered is not None
    assert abs(lossless.value - jittered.value) < 1e-4


def test_mid_frame_moves_a_cut_off_the_frame_timestamps():
    fps = 24000 / 1001
    for start in (0.0417, 7.3, 1234.5678):
        aligned = encoder._mid_frame(start, fps)
        frames = aligned * fps
        assert abs(frames - int(frames) - 0.5) < 1e-6
        assert abs(aligned - start) <= 1 / fps
    assert encoder._mid_frame(12.0, 0.0) == 12.0


# --- 2. cancelling the helper processes ------------------------------------------ #

def test_quality_check_hands_the_cancel_event_to_every_ffmpeg(monkeypatch):
    cancel = asyncio.Event()
    cuts, measured = [], []

    async def cut(source, start, duration, dest, **kw):
        cuts.append((start, kw.get("cancel_event")))
        Path(dest).write_bytes(b"x")

    async def measure(ref, dist, **kw):
        measured.append(kw.get("cancel_event"))
        return quality.QualityScore(0.99, "ssim", 96.0)

    monkeypatch.setattr(encoder.ffmpeg, "extract_segment", cut)
    monkeypatch.setattr(encoder.quality, "measure_quality", measure)
    score = asyncio.run(encoder._spot_check_quality("/lib/a.mkv", "/tmp/b.mkv", _info(), cancel))
    assert score is not None
    assert cuts and all(event is cancel for _, event in cuts)
    assert measured and all(event is cancel for event in measured)
    fps = 24000 / 1001
    for start, _ in cuts:
        assert abs(start * fps - int(start * fps) - 0.5) < 1e-6


def test_a_cancelled_cut_ends_the_quality_check_as_cancelled(monkeypatch):
    async def cut(*a, **kw):
        raise ffmpeg.FFmpegCancelled("abgebrochen", -9)

    measure = AsyncMock()
    monkeypatch.setattr(encoder.ffmpeg, "extract_segment", cut)
    monkeypatch.setattr(encoder.quality, "measure_quality", measure)
    with pytest.raises(encoder.JobCancelled):
        asyncio.run(encoder._spot_check_quality("/lib/a.mkv", "/tmp/b.mkv", _info(), asyncio.Event()))
    measure.assert_not_awaited()


def test_a_cancelled_comparison_ends_the_quality_check_as_cancelled(monkeypatch):
    async def cut(source, start, duration, dest, **kw):
        Path(dest).write_bytes(b"x")

    monkeypatch.setattr(encoder.ffmpeg, "extract_segment", cut)
    monkeypatch.setattr(encoder.quality, "measure_quality",
                        AsyncMock(side_effect=ffmpeg.FFmpegCancelled("abgebrochen", -9)))
    with pytest.raises(encoder.JobCancelled):
        asyncio.run(encoder._spot_check_quality("/lib/a.mkv", "/tmp/b.mkv", _info(), asyncio.Event()))


def test_a_cancel_during_the_quality_check_is_a_cancel_not_a_rejection(tmp_path, monkeypatch):
    source = tmp_path / "lib" / "Film.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x" * 1000)
    st = os.stat(source)
    plan = encoder.planner.EncodePlan(audio=[{"index": 1, "action": "copy", "codec": "ac3"}])
    with db.session_scope() as s:
        media = MediaFile(path=str(source), video_codec="hevc", state=FileState.QUEUED.value,
                          size=st.st_size, mtime=st.st_mtime, plan=plan.to_dict())
        s.add(media)
        s.flush()
        job = Job(file_id=media.id, plan=plan.to_dict(), state=JobState.QUEUED.value)
        s.add(job)
        s.flush()
        job_id = job.id
    cancel = asyncio.Event()

    async def fake_encode(plan, info, dest, job_id, settings, cancel_event):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"y" * 500)
        return 0, ""

    async def cut(*a, **kw):
        # The user presses cancel while the slice is being cut: the running
        # ffmpeg is stopped and reports it.
        assert kw.get("cancel_event") is cancel
        cancel.set()
        raise ffmpeg.FFmpegCancelled("abgebrochen", -9)

    info = _info(path=str(source), size=1000,
                 audio_streams=[{"index": 1, "codec": "ac3", "channels": 6}])
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=info))
    monkeypatch.setattr(encoder.ffmpeg, "extract_segment", cut)
    monkeypatch.setattr(encoder.quality, "verify_output", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(encoder, "_run_encode", fake_encode)
    commit = Mock(return_value=str(source))
    monkeypatch.setattr(encoder, "_commit_output", commit)
    cfg = AppSettings()
    cfg.analysis.use_learning_model = False
    cfg.queue.min_free_disk_gb = 0
    cfg.output.verify_vmaf = True
    cfg.output.min_accept_saving_percent = 0
    cfg.output.original_action = "delete"
    cfg.output.set_permissions = False
    outcome = asyncio.run(encoder.run_job(job_id, cfg, None, cancel))
    assert not commit.called
    assert outcome.reason == "Job abgebrochen"
    with db.session_scope() as s:
        assert s.get(Job, job_id).state == JobState.CANCELLED.value


def test_measurements_pass_the_cancel_event_and_do_not_retry_after_it(monkeypatch):
    calls = []

    async def run_simple(args, timeout=None, cancel_event=None):
        calls.append(cancel_event)
        raise ffmpeg.FFmpegCancelled("abgebrochen", -9)

    cancel = asyncio.Event()
    monkeypatch.setattr(quality, "available_metric", AsyncMock(return_value="vmaf"))
    monkeypatch.setattr(quality.ffmpeg, "run_simple", run_simple)
    with pytest.raises(ffmpeg.FFmpegCancelled):
        asyncio.run(quality.measure_quality(
            "ref.mkv", "dist.mkv", width=1920, height=1080, cancel_event=cancel,
        ))
    # No SSIM attempt after a cancelled VMAF run.
    assert calls == [cancel]

    calls.clear()
    with pytest.raises(ffmpeg.FFmpegCancelled):
        asyncio.run(quality.measure_grain("seg.mkv", cancel_event=cancel))
    assert calls == [cancel]


# --- 4. what the old default recycle folder still holds -------------------------- #

def test_purge_preserves_unrecorded_legacy_trash(tmp_path):
    old = tmp_path / "config" / "trash" / "2020-01-01" / "Serie" / "x.mkv"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x")
    os.utime(old, (0, 0))
    cfg = AppSettings()
    cfg.output.trash_dir = ""  # after the migration
    cfg.output.trash_retention_days = 14
    assert encoder.purge_trash(cfg) == 0
    assert old.exists()


# --- filter list of newer ffmpeg builds ------------------------------------------ #

@pytest.mark.parametrize("listing", [
    # ffmpeg 7 and jellyfin-ffmpeg: timeline, slice threads, commands
    " TSC ssim              VV->V      Calculate the SSIM between two video streams.\n"
    " ... libvmaf           VV->V      Calculate the VMAF between two video streams.\n",
    # newer builds dropped the command column
    " TS ssim              VV->V      Calculate the SSIM between two video streams.\n"
    " .. libvmaf           VV->V      Calculate the VMAF between two video streams.\n",
])
def test_filter_list_is_read_with_either_column_layout(monkeypatch, listing):
    async def fake_run(cmd, timeout=None, cancel_event=None):
        return 0, "Filters:\n  T.. = Timeline support\n  ------\n" + listing, ""

    monkeypatch.setattr(ffmpeg, "_filter_cache", None)
    monkeypatch.setattr(ffmpeg, "_run", fake_run)
    assert {"ssim", "libvmaf"} <= asyncio.run(ffmpeg.available_filters())
