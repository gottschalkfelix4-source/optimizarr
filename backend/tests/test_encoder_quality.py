"""Quality measurement and the output sanity check."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(Path(tempfile.gettempdir()) / "optimizarr-pytest/config"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import ffmpeg, planner, quality
from app.core.ffmpeg import MediaInfo


def test_compare_graph_scales_to_the_known_size_with_named_pads():
    graph = quality.build_compare_graph("ssim", 1920, 1080)
    assert "rw" not in graph and "rh" not in graph
    assert graph.startswith("[0:v:0]") and "[1:v:0]" in graph
    assert "scale=1920:1080" in graph
    assert graph.endswith("[dist][ref]ssim")
    assert graph.count(f"format={quality.COMPARE_PIX_FMT}") == 2


def test_measure_quality_hands_the_reference_size_to_the_graph(monkeypatch):
    seen = []

    async def fake_run(args, timeout=None, cancel_event=None):
        seen.append(args[args.index("-lavfi") + 1])
        return 0, "", "[Parsed_ssim_4 @ 0x1] SSIM Y:0.99 All:0.985 (18.2)"

    monkeypatch.setattr(quality, "available_metric", AsyncMock(return_value="ssim"))
    monkeypatch.setattr(quality.ffmpeg, "run_simple", fake_run)
    score = asyncio.run(quality.measure_quality("ref.mkv", "dist.mkv", width=3840, height=2160))
    assert score is not None and score.metric == "ssim"
    assert "scale=3840:2160" in seen[0]


def _have_ffmpeg():
    return ffmpeg.FFMPEG and shutil.which(ffmpeg.FFMPEG) or os.path.exists(ffmpeg.FFMPEG or "")


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not installed")
@pytest.mark.parametrize("metric", ["vmaf", "ssim"])
def test_real_ffmpeg_measures_a_downscaled_10bit_encode(tmp_path, monkeypatch, metric):
    ref = tmp_path / "ref.mkv"
    dist = tmp_path / "dist.mkv"
    base = [ffmpeg.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", "testsrc2=size=640x360:rate=24:duration=2"]
    subprocess.run(base + ["-pix_fmt", "yuv420p", "-c:v", "ffv1", str(ref)], check=True)
    subprocess.run(base + ["-vf", "scale=320:180", "-pix_fmt", "yuv420p10le", "-c:v", "ffv1",
                           str(dist)], check=True)
    filters = asyncio.run(ffmpeg.available_filters())
    if metric == "vmaf" and "libvmaf" not in filters:
        pytest.skip("ffmpeg without libvmaf")
    monkeypatch.setattr(quality, "available_metric", AsyncMock(return_value=metric))
    score = asyncio.run(quality.measure_quality(str(ref), str(dist), threads=2, timeout=120))
    assert score is not None, "the comparison graph must run on a real ffmpeg"
    assert score.metric == metric
    assert 0 < score.vmaf_estimate <= 100


# --- verify_output ------------------------------------------------------------ #

def _out(tmp_path):
    out = tmp_path / "out.mkv"
    out.write_bytes(b"x" * 4096)
    return out


def _info(**kw):
    values = dict(path="/lib/a.mkv", duration=600.0, video_codec="av1",
                  audio_streams=[{"index": 1, "codec": "opus"}], subtitle_streams=[])
    values.update(kw)
    return MediaInfo(**values)


def _video_raw(duration_tag):
    return {"streams": [{"codec_type": "video", "tags": {"DURATION": duration_tag}},
                        {"codec_type": "audio", "tags": {"DURATION": "00:10:00.000000000"}}]}


def test_output_with_a_truncated_video_stream_is_rejected(tmp_path, monkeypatch):
    out = _out(tmp_path)
    monkeypatch.setattr(quality.ffmpeg, "probe", AsyncMock(return_value=_info(
        raw=_video_raw("00:04:00.000000000"))))
    ok, reason = asyncio.run(quality.verify_output(_info(video_codec="hevc"), str(out)))
    assert not ok and "Videospur" in reason


def test_output_with_a_complete_video_stream_passes(tmp_path, monkeypatch):
    out = _out(tmp_path)
    monkeypatch.setattr(quality.ffmpeg, "probe", AsyncMock(return_value=_info(
        raw=_video_raw("00:09:59.960000000"))))
    ok, _ = asyncio.run(quality.verify_output(_info(video_codec="hevc"), str(out)))
    assert ok


def test_output_missing_a_planned_track_is_rejected(tmp_path, monkeypatch):
    out = _out(tmp_path)
    plan = planner.EncodePlan(
        audio=[{"index": 1, "action": "copy"}, {"index": 2, "action": "opus"},
               {"index": 3, "action": "drop"}],
        subtitles=[{"index": 4, "action": "copy"}],
    )
    source = _info(video_codec="hevc", audio_streams=[{"index": i} for i in (1, 2, 3)],
                   subtitle_streams=[{"index": 4}])
    monkeypatch.setattr(quality.ffmpeg, "probe", AsyncMock(return_value=_info(
        audio_streams=[{"index": 1}], subtitle_streams=[{"index": 2}])))
    ok, reason = asyncio.run(quality.verify_output(source, str(out), plan=plan))
    assert not ok and "Tonspur" in reason and "2" in reason

    monkeypatch.setattr(quality.ffmpeg, "probe", AsyncMock(return_value=_info(
        audio_streams=[{"index": 1}, {"index": 2}], subtitle_streams=[])))
    ok, reason = asyncio.run(quality.verify_output(source, str(out), plan=plan))
    assert not ok and "Untertitel" in reason

    monkeypatch.setattr(quality.ffmpeg, "probe", AsyncMock(return_value=_info(
        audio_streams=[{"index": 1}, {"index": 2}], subtitle_streams=[{"index": 3}])))
    assert asyncio.run(quality.verify_output(source, str(out), plan=plan))[0]
