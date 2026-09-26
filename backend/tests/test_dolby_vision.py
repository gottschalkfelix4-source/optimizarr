"""Dolby Vision detection - ffprobe calls the side data "DOVI configuration record"."""
from app.core import ffmpeg


def _stream(**kw):
    base = {"codec_type": "video", "codec_name": "hevc"}
    base.update(kw)
    return base


def test_profile5_without_transfer_is_detected():
    s = _stream(side_data_list=[{"side_data_type": "DOVI configuration record", "dv_profile": 5}])
    assert ffmpeg._detect_hdr(s) == (True, "dolby_vision_p5")


def test_profile8_with_hdr10_base_is_dolby_vision():
    s = _stream(
        color_transfer="smpte2084",
        side_data_list=[{"side_data_type": "DOVI configuration record", "dv_profile": 8}],
    )
    assert ffmpeg._detect_hdr(s) == (True, "dolby_vision_p8")


def test_codec_tag_without_record_is_unknown_profile():
    assert ffmpeg._detect_hdr(_stream(codec_tag_string="dvh1")) == (True, "dolby_vision")


def test_plain_hdr10_and_sdr_unchanged():
    assert ffmpeg._detect_hdr(_stream(color_transfer="smpte2084")) == (True, "hdr10")
    assert ffmpeg._detect_hdr(_stream(color_transfer="bt709")) == (False, "")


def test_helpers():
    assert ffmpeg.is_dolby_vision("dolby_vision_p5")
    assert ffmpeg.dolby_vision_profile("dolby_vision_p8") == 8
    assert ffmpeg.dolby_vision_profile("dolby_vision") == 0
    assert ffmpeg.dolby_vision_profile("hdr10") is None
    assert not ffmpeg.is_dolby_vision("")
