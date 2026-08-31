"""Regression tests for the Intel hardware path.

Every assertion here corresponds to a way an Arc A380 was observed falling back
to the CPU, or to a silent correctness bug found while chasing it.
"""
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
from app.core import planner  # noqa: E402
from app.core.ffmpeg import MediaInfo, first_error_line  # noqa: E402
from app.core.hwaccel import EncoderCapability, HardwareReport  # noqa: E402


def make_info(**kw) -> MediaInfo:
    base = dict(
        path="/media/Film.mkv", container="matroska", size=12 * 1024**3, duration=7200.0,
        video_codec="h264", width=1920, height=1080, fps=23.976,
        video_bitrate=12_000_000, bit_depth=8, pix_fmt="yuv420p",
        audio_streams=[], subtitle_streams=[],
    )
    base.update(kw)
    return MediaInfo(**base)


def arc_report(decode_ok: bool | None = True) -> HardwareReport:
    """A report shaped like a working Arc A380."""
    rep = HardwareReport(
        device="/dev/dri/renderD128", device_present=True, readable=True,
        gpu_name="Intel Arc (Alchemist) [0x56a6]",
        decode_h264=True, decode_hevc=True, decode_vp9=True, decode_av1=True,
        recommended_encoder="av1_qsv", hw_decode_usable=decode_ok,
    )
    for name in ("av1_qsv", "av1_vaapi", "hevc_qsv", "libsvtav1"):
        rep.encoders[name] = EncoderCapability(name=name, available=True, verified=True)
    return rep


def qsv_command(settings: AppSettings | None = None, info: MediaInfo | None = None) -> str:
    settings = settings or AppSettings()
    info = info or make_info()
    plan = planner.build_plan(info, settings, hw=arc_report())
    return " ".join(planner.build_ffmpeg_args(plan, info, info.path, "/tmp/out.mkv"))


# --------------------------------------------------------------------------- #
# Parameters that broke the encoder on Arc
# --------------------------------------------------------------------------- #

def test_extbrc_is_never_sent():
    """Reported to fail encoder init outright on DG2/Arc, and useless with ICQ."""
    assert "-extbrc" not in qsv_command()


def test_look_ahead_depth_is_never_sent():
    """A no-op with -global_quality, and actively harmful in the modes where it
    does apply unless async_depth is pinned to 1."""
    assert "-look_ahead_depth" not in qsv_command()


def test_low_power_is_still_sent():
    """Not a fix - it silences a misleading 'unsupported' line, and the smoke
    test that passes on this hardware includes it."""
    assert "-low_power 1" in qsv_command()


def test_quality_and_bitrate_are_never_combined():
    """Together they select a rate-control mode that AV1 has no Linux
    implementation for, which fails every time."""
    command = qsv_command()
    assert "-global_quality" in command
    assert "-b:v" not in command
    assert "-maxrate" not in command


# --------------------------------------------------------------------------- #
# The silent 10-bit bug
# --------------------------------------------------------------------------- #

def test_gpu_decode_path_converts_the_pixel_format():
    """Without this the plan says 10-bit and the encode quietly produces 8-bit.

    The QSV encoder reads the format off the incoming surfaces and never
    converts it, so a conversion stage has to run even when there is nothing to
    scale or deinterlace.
    """
    command = qsv_command()
    assert "-hwaccel qsv" in command          # decoding on the GPU
    assert "vpp_qsv=format=p010le" in command  # and the format actually enforced


def test_conversion_stage_runs_without_scaling_or_deinterlacing():
    settings = AppSettings()
    settings.encoding.max_width = 0        # no scaling
    settings.encoding.deinterlace = False  # no deinterlacing
    info = make_info(interlaced=False)
    plan = planner.build_plan(info, settings, hw=arc_report())
    args = planner.build_ffmpeg_args(plan, info, info.path, "/tmp/out.mkv")
    assert "-vf" in args, "a filter chain must exist even with nothing to filter"
    assert "vpp_qsv=format=p010le" in args[args.index("-vf") + 1]


def test_only_one_vpp_instance_even_with_scaling_and_deinterlacing():
    """Two vpp_qsv stages, or a software format= in a surface chain, break the
    graph - everything has to fold into a single instance."""
    settings = AppSettings()
    settings.encoding.max_width = 1280
    info = make_info(interlaced=True, width=1920, height=1080)
    plan = planner.build_plan(info, settings, hw=arc_report())
    args = planner.build_ffmpeg_args(plan, info, info.path, "/tmp/out.mkv")
    chain = args[args.index("-vf") + 1]
    assert chain.count("vpp_qsv") == 1
    assert "deinterlace=2" in chain
    assert "format=p010le" in chain
    # A software converter would insert an auto-scaler that cannot bridge
    # hardware and software formats.
    assert not chain.startswith("format=")


def test_vaapi_also_converts_without_scaling():
    settings = AppSettings()
    settings.encoding.encoder = "av1_vaapi"
    info = make_info()
    plan = planner.build_plan(info, settings, hw=arc_report())
    plan.hw_decode = True
    args = planner.build_ffmpeg_args(plan, info, info.path, "/tmp/out.mkv")
    chain = args[args.index("-vf") + 1]
    assert "scale_vaapi" in chain
    # VAAPI spells the 10-bit format without the endianness suffix.
    assert "format=p010" in chain and "p010le" not in chain


def test_software_decode_path_still_converts_before_upload():
    settings = AppSettings()
    settings.hardware.hw_decode = False
    info = make_info()
    plan = planner.build_plan(info, settings, hw=arc_report())
    args = planner.build_ffmpeg_args(plan, info, info.path, "/tmp/out.mkv")
    chain = args[args.index("-vf") + 1]
    assert chain.startswith("format=p010le")
    assert "hwupload" in chain


# --------------------------------------------------------------------------- #
# Frame pool
# --------------------------------------------------------------------------- #

def test_extra_hw_frames_is_set_for_gpu_decoding():
    """Pools are fixed at allocation and the default is tight for a full
    hardware transcode."""
    args = qsv_command().split()
    assert "-extra_hw_frames" in args
    # It is an input option: it has to come before -i, or it is ignored.
    assert args.index("-extra_hw_frames") < args.index("-i")


def test_no_extra_hw_frames_without_gpu_decoding():
    settings = AppSettings()
    settings.hardware.hw_decode = False
    assert "-extra_hw_frames" not in qsv_command(settings)


# --------------------------------------------------------------------------- #
# Independent verdicts for the two halves
# --------------------------------------------------------------------------- #

def test_broken_decode_path_keeps_the_encoder_on_the_gpu():
    """Losing GPU decoding must not cost the expensive half too."""
    settings = AppSettings()
    info = make_info()
    plan = planner.build_plan(info, settings, hw=arc_report(decode_ok=False))
    assert plan.encoder == "av1_qsv"   # still on the GPU
    assert plan.hw_decode is False     # but feeding it from the CPU
    assert any("CPU" in note for note in plan.notes)

    command = " ".join(planner.build_ffmpeg_args(plan, info, info.path, "/tmp/out.mkv"))
    assert "-hwaccel qsv" not in command
    assert "format=p010le" in command and "hwupload" in command


def test_unprobed_decode_path_is_still_allowed():
    """None means 'not probed', which must not read as 'broken'."""
    plan = planner.build_plan(make_info(), AppSettings(), hw=arc_report(decode_ok=None))
    assert plan.hw_decode is True


def test_encoder_args_are_shared_with_the_probe():
    """The probe has to run what production runs, or it verifies nothing."""
    from app.core import hwaccel

    args = planner.qsv_encoder_args(30, 6, 120, low_power=True)
    assert args[:2] == ["-c:v", "av1_qsv"]
    assert "-extbrc" not in args
    assert "-look_ahead_depth" not in args
    # hwaccel must not build its own copy of these.
    source = Path(hwaccel.__file__).read_text(encoding="utf-8")
    assert "qsv_encoder_args" in source
    assert '"-c:v", "av1_qsv"' not in source


# --------------------------------------------------------------------------- #
# Error reporting
# --------------------------------------------------------------------------- #

def test_error_line_picks_the_cause_not_the_summary():
    """ffmpeg names the cause first and a vague summary afterwards; reading
    backwards returns the useless half."""
    log = (
        "frame= 42 fps=12 q=-0.0 size=0kB\n"
        "[av1_qsv @ 0x55] Selected ratecontrol mode is unsupported\n"
        "[vost#0:0/av1_qsv @ 0x55] Error while opening encoder - maybe incorrect parameters\n"
        "Conversion failed!\n"
    )
    assert "ratecontrol" in first_error_line(log)


def test_error_line_skips_noise():
    log = "Last message repeated 5 times\n[hwupload] Cannot allocate memory\n"
    assert "Cannot allocate memory" in first_error_line(log)


def test_error_line_survives_empty_input():
    assert first_error_line("") == "keine Fehlermeldung von ffmpeg"
    assert first_error_line("nur harmlose Ausgabe") == "nur harmlose Ausgabe"


# --------------------------------------------------------------------------- #
# Telling cause from consequence
#
# When one stream fails, ffmpeg tears the pipeline down and every other stream
# reports its own error.  Retrying on the CPU only helps if the *video* encoder
# is what broke - otherwise it burns hours to fail the same way.
# --------------------------------------------------------------------------- #

REAL_AUDIO_FAILURE = """\
[aost#0:1/libopus @ 0x55a944bc2240] Error while opening encoder - maybe incorrect \
parameters such as bit_rate, rate, width or height.
[af#0:1 @ 0x55a944a52a40] Error sending frames to consumers: Invalid argument
[af#0:2 @ 0x55a944b3d7c0] Task finished with error code: -22 (Invalid argument)
[aost#0:1/libopus @ 0x55a944bc2240] Could not open encoder before EOF
[aost#0:2/libopus @ 0x55a944b4c480] Could not open encoder before EOF
[out#0/matroska @ 0x55a944a97400] Nothing was written into output file, because \
at least one of its streams received no packets.
Conversion failed!
"""


def test_audio_failure_is_not_blamed_on_the_gpu():
    """Taken verbatim from a run that fell back to the CPU for no reason."""
    from app.core.ffmpeg import failure_is_video, first_error_line

    assert failure_is_video(REAL_AUDIO_FAILURE) is False
    assert "libopus" in first_error_line(REAL_AUDIO_FAILURE)


def test_video_failure_is_recognised_even_when_audio_shouts_louder():
    from app.core.ffmpeg import failure_is_video, first_error_line

    log = (
        "[av1_qsv @ 0x1] Selected ratecontrol mode is unsupported\n"
        "[vost#0:0/av1_qsv @ 0x1] Error while opening encoder\n"
        + REAL_AUDIO_FAILURE
    )
    assert failure_is_video(log) is True
    # The video line is the useful one even though audio produced more output.
    assert "ratecontrol" in first_error_line(log)


def test_device_failure_counts_as_video():
    from app.core.ffmpeg import failure_is_video

    assert failure_is_video("[AVHWDeviceContext @ 0x1] Error creating a QSV device") is True
    assert failure_is_video("Device creation failed: -22.") is True


def test_inconclusive_log_defaults_to_retrying():
    """With nothing to go on, the CPU retry is the safer guess."""
    from app.core.ffmpeg import failure_is_video

    assert failure_is_video("") is True
    assert failure_is_video("frame=  100 fps=25 q=-0.0") is True


def test_consequences_alone_leave_the_verdict_open():
    """A log with only follow-on errors names no culprit.

    "Could not open encoder before EOF" appears on every stream once the
    pipeline collapses, so it identifies nothing.  With no evidence either way
    the CPU retry is the safer guess - it costs time, while wrongly refusing it
    would leave a file unconverted.
    """
    from app.core.ffmpeg import failure_is_video

    log = (
        "[aost#0:1/libopus @ 0x1] Could not open encoder before EOF\n"
        "Conversion failed!\n"
    )
    assert failure_is_video(log) is True

    # But as soon as one line names a stream, that decides it.
    with_cause = "[aost#0:1/libopus @ 0x1] Error while opening encoder\n" + log
    assert failure_is_video(with_cause) is False
