"""Planner: hardware quality scales, decode formats, CPU limits, Dolby Vision.

The quality mappings were calibrated on an Arc A380 (see planner.hw_quality);
these tests pin the behaviour that was found broken live:

* av1_vaapi ignored ``-qp`` and encoded everything at q_idx 25,
* av1_qsv read the CRF on its own, much steeper ICQ scale,
* GPU decoding for the CPU encoder downloaded 10-bit frames as 8-bit nv12,
* ``lp=<threads>`` means "level of parallelism" in current SVT-AV1 and
  limited nothing.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.gettempdir()) / "optimizarr-pytest"
os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(TMP / "config"))
os.environ.setdefault("OPTIMIZARR_TRANSCODE_DIR", str(TMP / "transcode"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AppSettings  # noqa: E402
from app.core import ffmpeg, hwaccel, planner  # noqa: E402
from app.core.ffmpeg import MediaInfo  # noqa: E402
from app.core.hwaccel import EncoderCapability, HardwareReport  # noqa: E402


def make_info(**kw) -> MediaInfo:
    base = dict(
        path="/media/Film.mkv", container="matroska", size=8 * 1024**3, duration=5400.0,
        video_codec="hevc", width=1920, height=1080, fps=23.976,
        video_bitrate=10_000_000, bit_depth=8, pix_fmt="yuv420p",
        audio_streams=[], subtitle_streams=[],
    )
    base.update(kw)
    return MediaInfo(**base)


def hdr10_info(**kw) -> MediaInfo:
    base = dict(bit_depth=10, pix_fmt="yuv420p10le", is_hdr=True, hdr_format="hdr10",
                color_primaries="bt2020", color_transfer="smpte2084", color_space="bt2020nc")
    base.update(kw)
    return make_info(**base)


def arc_report() -> HardwareReport:
    rep = HardwareReport(
        device="/dev/dri/renderD128", device_present=True, readable=True,
        decode_h264=True, decode_hevc=True, decode_vp9=True, decode_av1=True,
        recommended_encoder="av1_qsv", hw_decode_usable=True,
    )
    for name in ("av1_qsv", "av1_vaapi", "libsvtav1"):
        rep.encoders[name] = EncoderCapability(name=name, available=True, verified=True)
    return rep


def build(encoder: str, info: MediaInfo | None = None, crf: float | None = None,
          **settings_kw) -> tuple[planner.EncodePlan, list[str]]:
    settings = AppSettings()
    settings.encoding.encoder = encoder
    for key, value in settings_kw.items():
        group, _, name = key.partition("__")
        setattr(getattr(settings, group), name, value)
    info = info or make_info()
    plan = planner.build_plan(info, settings, hw=arc_report(), crf=crf)
    return plan, planner.build_ffmpeg_args(plan, info, info.path, "/tmp/out.mkv")


def value_of(args: list[str], option: str) -> str:
    return args[args.index(option) + 1]


# --------------------------------------------------------------------------- #
# Quality scales
# --------------------------------------------------------------------------- #

def test_vaapi_uses_constant_qp_through_global_quality():
    _, args = build("av1_vaapi", crf=30)
    assert "-qp:v" not in args and "-qp" not in args
    assert value_of(args, "-rc_mode:v") == "CQP"
    assert value_of(args, "-global_quality:v") == "90"


def test_vaapi_quality_actually_follows_the_crf():
    """Every CRF used to produce the same file."""
    values = {value_of(build("av1_vaapi", crf=c)[1], "-global_quality:v") for c in (20, 30, 40)}
    assert values == {"60", "90", "120"}


def test_qsv_translates_crf_onto_its_icq_scale():
    _, args = build("av1_qsv", crf=30)
    assert value_of(args, "-global_quality:v") == "26"


@pytest.mark.parametrize("encoder, low, high", [("av1_vaapi", 1, 255), ("av1_qsv", 1, 51)])
def test_hw_quality_is_monotonic_and_in_range(encoder, low, high):
    values = [planner.hw_quality(c, encoder) for c in range(0, 64)]
    assert values == sorted(values)
    assert low <= min(values) and max(values) <= high
    assert all(isinstance(v, int) for v in values)


def test_calibration_points():
    """The measured equal-VMAF points (planner.hw_quality docstring)."""
    assert [planner.hw_quality(c, "av1_vaapi") for c in (22, 26, 30, 34, 38)] == [66, 78, 90, 102, 114]
    assert [planner.hw_quality(c, "av1_qsv") for c in (22, 26, 30, 34, 38)] == [22, 24, 26, 28, 29]
    assert planner.hw_quality(30, "libsvtav1") == 30


def test_plan_description_shows_the_hardware_value():
    plan, _ = build("av1_vaapi", crf=30)
    assert "CRF 30 (q_idx 90)" in plan.describe()
    plan, _ = build("av1_qsv", crf=30)
    assert "CRF 30 (ICQ 26)" in plan.describe()


def test_dead_qp_helper_is_gone():
    assert not hasattr(planner, "qp_for_encoder")


# --------------------------------------------------------------------------- #
# The hardware probe runs what production runs
# --------------------------------------------------------------------------- #

def _capture_run_simple(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    async def fake(args, timeout=None, cancel_event=None):
        calls.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(ffmpeg, "run_simple", fake)
    return calls


def test_vaapi_smoke_test_matches_production(monkeypatch):
    calls = _capture_run_simple(monkeypatch)
    ok, _ = asyncio.run(hwaccel._smoke_test("av1_vaapi", "/dev/dri/renderD129", True))
    assert ok
    args = calls[0]
    assert "-vaapi_device" not in args and "-qp" not in args
    assert value_of(args, "-init_hw_device") == "vaapi=va:/dev/dri/renderD129"
    assert value_of(args, "-filter_hw_device") == "va"
    assert value_of(args, "-rc_mode:v") == "CQP"
    assert args[args.index("-c:v"):args.index("-c:v") + 8] == planner.vaapi_encoder_args(30, 120)


@pytest.mark.parametrize("encoder", ["av1_qsv", "av1_vaapi"])
def test_decode_path_probe_is_the_production_command(monkeypatch, encoder):
    """Built by build_ffmpeg_args itself - and now for VAAPI too, which never
    had its GPU decode path checked."""
    calls = _capture_run_simple(monkeypatch)
    ok, _ = asyncio.run(hwaccel._decode_path_test(encoder, "/dev/dri/renderD128", True, "p010le"))
    assert ok
    probe_args = calls[1]
    source = probe_args[probe_args.index("-i") + 1]
    dest = probe_args[-1]
    info = MediaInfo(path=source, container="mp4", duration=5.0, video_codec="h264",
                     width=1920, height=1080, fps=24.0, bit_depth=8, pix_fmt="yuv420p")
    plan = planner.EncodePlan(encoder=encoder, crf=30, preset=6, pix_fmt="p010le", hw_decode=True,
                              hw_device="/dev/dri/renderD128", low_power=True, keyint_frames=120)
    assert probe_args == planner.build_ffmpeg_args(plan, info, source, dest, quiet_streams=True)
    assert "-hwaccel" in probe_args


def test_detect_checks_the_vaapi_decode_path(monkeypatch):
    """With only VAAPI verified the decode verdict stayed 'not probed'."""
    seen = []

    async def fake_decode(encoder, device, low_power, pix_fmt):
        seen.append(encoder)
        return False, "kaputt"

    async def fake_smoke(encoder, device, low_power, pix_fmt="p010le"):
        return encoder in ("av1_vaapi", "libsvtav1"), "" if encoder != "av1_qsv" else "nope"

    async def fake_vainfo(device):
        return True, "iHD", ["VAProfileAV1Profile0 : VAEntrypointEncSliceLP",
                             "VAProfileHEVCMain10 : VAEntrypointVLD"], ""

    async def names(refresh=False):
        return {"libsvtav1", "av1_qsv", "av1_vaapi", "hevc_qsv", "ssim"}

    async def version():
        return "ffmpeg test"

    monkeypatch.setattr(hwaccel, "_report", None)
    monkeypatch.setattr(hwaccel, "_decode_path_test", fake_decode)
    monkeypatch.setattr(hwaccel, "_smoke_test", fake_smoke)
    monkeypatch.setattr(hwaccel, "_run_vainfo", fake_vainfo)
    monkeypatch.setattr(hwaccel.ffmpeg, "available_encoders", names)
    monkeypatch.setattr(hwaccel.ffmpeg, "available_filters", names)
    monkeypatch.setattr(hwaccel.ffmpeg, "version", version)
    monkeypatch.setattr(hwaccel.os, "access", lambda *a, **k: True)
    monkeypatch.setattr(hwaccel.Path, "exists", lambda self: True)

    rep = asyncio.run(hwaccel.detect("/dev/dri/renderD128", force=True))
    assert rep.recommended_encoder == "av1_vaapi"
    assert seen == ["av1_vaapi"]
    assert rep.hw_decode_usable is False


def test_dead_decode_arg_builder_is_gone():
    assert not hasattr(hwaccel, "build_decode_args")
    assert "nv12" not in Path(hwaccel.__file__).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# GPU decode feeding the CPU encoder
# --------------------------------------------------------------------------- #

def test_gpu_decode_for_cpu_encode_keeps_10_bit():
    """-hwaccel_output_format nv12 downloaded HDR as 8 bit before the 10-bit encode."""
    plan, args = build("svt_av1", info=hdr10_info())
    assert plan.hw_decode is True
    assert args[:args.index("-i")].count("-hwaccel") == 1
    assert "-hwaccel_output_format" not in args
    assert "nv12" not in " ".join(args)
    assert value_of(args, "-vf").endswith("format=yuv420p10le")
    assert value_of(args, "-color_trc:v") == "smpte2084"


def test_gpu_decode_for_cpu_encode_on_8_bit_sources():
    _, args = build("svt_av1", info=make_info(video_codec="h264"))
    assert "-hwaccel" in args and "-hwaccel_output_format" not in args


def test_full_gpu_path_keeps_10_bit_hdr():
    _, args = build("av1_vaapi", info=hdr10_info())
    assert value_of(args, "-hwaccel_output_format") == "vaapi"
    assert "format=p010" in value_of(args, "-vf")
    assert value_of(args, "-color_primaries:v") == "bt2020"


# --------------------------------------------------------------------------- #
# CPU limit
# --------------------------------------------------------------------------- #

def test_cpu_threads_become_a_core_limit_not_lp(monkeypatch):
    monkeypatch.setattr(planner, "cpu_limit", lambda threads: min(threads, 16) if threads else 0)
    _, args = build("svt_av1", queue__cpu_threads=8)
    params = value_of(args, "-svtav1-params:v")
    assert "lp=" not in params
    assert "pin=8" in params.split(":")
    assert value_of(args, "-threads") == "8"
    assert args.index("-threads") < args.index("-i")


def test_cpu_limit_never_exceeds_the_machine():
    available = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    assert planner.cpu_limit(100_000) == available
    assert planner.cpu_limit(1) == 1
    assert planner.cpu_limit(0) == 0


def test_no_cpu_limit_by_default():
    _, args = build("svt_av1")
    assert "pin=" not in value_of(args, "-svtav1-params:v")
    assert "-threads" not in args


# --------------------------------------------------------------------------- #
# Dolby Vision fallback
# --------------------------------------------------------------------------- #

def dv8_info() -> MediaInfo:
    return hdr10_info(hdr_format="dolby_vision_p8")


def test_dolby_vision_fallback_keeps_the_hdr10_base():
    plan, args = build("svt_av1", info=dv8_info())
    assert plan.pix_fmt == "yuv420p10le"
    assert value_of(args, "-color_trc:v") == "smpte2084"
    assert value_of(args, "-color_primaries:v") == "bt2020"
    assert value_of(args, "-colorspace:v") == "bt2020nc"
    # No RPU is written next to a picture it no longer describes.
    assert value_of(args, "-dolbyvision:v") == "0"
    assert any("Dolby Vision" in note for note in plan.notes)


def test_dolby_vision_switch_only_where_needed():
    _, args = build("svt_av1", info=hdr10_info())
    assert "-dolbyvision:v" not in args
    # The GPU encoders cannot write RPUs at all (checked live: no DOVI record).
    _, args = build("av1_vaapi", info=dv8_info())
    assert "-dolbyvision:v" not in args
    assert value_of(args, "-color_trc:v") == "smpte2084"


def test_a_lock_left_held_by_a_dead_loop_does_not_block_detection(monkeypatch):
    """A detection cut off with its event loop must not wedge every later one."""
    async def hold_forever():
        await hwaccel._detect_lock().acquire()

    loop = asyncio.new_event_loop()
    loop.run_until_complete(hold_forever())
    loop.close()

    async def cached():
        return await asyncio.wait_for(hwaccel.detect("/dev/dri/renderD128"), timeout=2)

    monkeypatch.setattr(hwaccel, "_report", hwaccel.HardwareReport(device="/dev/dri/renderD128"))
    assert asyncio.run(cached()).device == "/dev/dri/renderD128"
