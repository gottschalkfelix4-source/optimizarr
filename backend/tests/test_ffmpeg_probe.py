"""probe() on awkward metadata: Matroska DURATION tags and Dolby Vision."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.gettempdir()) / "optimizarr-pytest"
os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(TMP / "config"))
os.environ.setdefault("OPTIMIZARR_TRANSCODE_DIR", str(TMP / "transcode"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import ffmpeg  # noqa: E402


def _probe_with(monkeypatch, video_tags=None, fmt=None, video_extra=None):
    video = {
        "index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
        "avg_frame_rate": "24000/1001", "pix_fmt": "yuv420p10le", "tags": video_tags or {},
    }
    video.update(video_extra or {})
    data = {
        "format": {"format_name": "matroska,webm", "size": "1000000", **(fmt or {})},
        "streams": [video, {"index": 1, "codec_type": "audio", "codec_name": "eac3",
                            "channels": 6, "bit_rate": "N/A", "sample_rate": "48000"}],
    }

    async def fake_run(cmd, timeout=None, cancel_event=None):
        return 0, json.dumps(data), ""

    monkeypatch.setattr(ffmpeg, "_run", fake_run)
    return asyncio.run(ffmpeg.probe("/media/x.mkv"))


@pytest.mark.parametrize("value, expected", [
    ("01:23:45.000000000", 5025.0),
    ("00:47:42.860000000", 2862.86),
    ("12:34.5", 754.5),
    ("5025.5", 5025.5),
    ("", 0.0),
    ("N/A", 0.0),
    ("garbage", 0.0),
    ("01:99:00.0", 0.0),
    ("1:2:3:4", 0.0),
    ("-00:00:05", 0.0),
    (None, 0.0),
])
def test_duration_tag_parsing(value, expected):
    assert ffmpeg.parse_duration_tag(value) == pytest.approx(expected)


def test_probe_reads_the_mkvmerge_duration_tag(monkeypatch):
    """float("01:23:45.000000000") raised ValueError and made the file unreadable."""
    info = _probe_with(monkeypatch, video_tags={"DURATION": "01:23:45.000000000"})
    assert info.duration == pytest.approx(5025.0)


def test_probe_reads_a_language_suffixed_duration_tag(monkeypatch):
    info = _probe_with(monkeypatch, video_tags={"DURATION-eng": "00:21:43.021000000"})
    assert info.duration == pytest.approx(1303.021)


def test_probe_survives_a_broken_duration_tag(monkeypatch):
    info = _probe_with(monkeypatch, video_tags={"DURATION": "banana"})
    assert info.duration == 0.0
    assert info.audio_streams[0]["bitrate"] == 0   # "N/A" does not raise either


def test_container_duration_still_wins(monkeypatch):
    info = _probe_with(monkeypatch, fmt={"duration": "100.5"},
                       video_tags={"DURATION": "01:00:00.000000000"})
    assert info.duration == pytest.approx(100.5)


def test_probe_reports_dolby_vision_profiles(monkeypatch):
    """Shapes taken from ffprobe on real library files (Ted Lasso S04E05 = P5,
    S04E04 = P8.1)."""
    p5 = _probe_with(monkeypatch, video_extra={"side_data_list": [{
        "side_data_type": "DOVI configuration record", "dv_profile": 5,
        "dv_bl_signal_compatibility_id": 0, "rpu_present_flag": 1}]})
    assert p5.hdr_format == "dolby_vision_p5" and p5.is_hdr
    p8 = _probe_with(monkeypatch, video_extra={"color_transfer": "smpte2084", "side_data_list": [{
        "side_data_type": "DOVI configuration record", "dv_profile": 8,
        "dv_bl_signal_compatibility_id": 1, "rpu_present_flag": 1}]})
    assert p8.hdr_format == "dolby_vision_p8"
    assert ffmpeg.dolby_vision_profile(p8.hdr_format) == 8
