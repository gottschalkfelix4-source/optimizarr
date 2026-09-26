"""Failure classification must read ffmpeg's diagnostics, not its header dump.

Before it does anything ffmpeg prints every input and output: paths, tags,
stream titles.  A keyword search over that found "errors" in an episode called
"Trial and Error" or a folder called "Invalid", and the verdict decided whether
a job was retried on the CPU.
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "optimizarr-pytest"
os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(TMP / "config"))
os.environ.setdefault("OPTIMIZARR_TRANSCODE_DIR", str(TMP / "transcode"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.ffmpeg import failure_is_video, first_error_line  # noqa: E402

# Shaped like jellyfin-ffmpeg 7.1 run with -hide_banner -nostdin -progress.
HEADER = """\
[hevc @ 0x5590a3c0] Invalid NAL unit 63, skipping.
Input #0, matroska,webm, from '/media/Serien/Invalid Media/Trial and Error (2017)/S01E01 - Failed Attempt.mkv':
  Metadata:
    title           : Trial and Error - Error Unable to Cannot
    encoder         : libebml v1.4.2 + libmatroska v1.6.4
  Duration: 00:21:43.02, start: 0.000000, bitrate: 6134 kb/s
  Chapters:
    Chapter #0:0: start 0.000000, end 90.000000
      Metadata:
        title           : Error in the pilot
  Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 1920x1080, SAR 1:1 DAR 16:9, 23.98 fps
      Metadata:
        title           : Unsupported Director's Cut
  Stream #0:1(ger): Audio: eac3, 48000 Hz, 5.1(side), fltp, 640 kb/s (default)
      Metadata:
        title           : Deutsch - Failed Dub
Stream mapping:
  Stream #0:0 -> #0:0 (hevc (native) -> av1 (av1_vaapi))
  Stream #0:1 -> #0:1 (eac3 (native) -> opus (libopus))
Output #0, matroska, to '/transcode/optimizarr-12-invalid.mkv':
  Metadata:
    title           : Trial and Error - Error Unable to Cannot
  Stream #0:0: Video: av1, vaapi(tv, bt2020nc/bt2020/smpte2084), 1920x1080, q=2-31
      Metadata:
        title           : Unsupported Director's Cut
"""

VIDEO_FAILURE = """\
[vaapi @ 0x5590a3f0] Failed to sync surface 0x4: 1 (operation failed).
[vost#0:0/av1_vaapi @ 0x5590a400] Error submitting video frame to the encoder
Conversion failed!
"""

AUDIO_FAILURE = """\
[aost#0:1/libopus @ 0x55a944bc2240] Error while opening encoder - maybe incorrect parameters
[af#0:1 @ 0x55a944a52a40] Error sending frames to consumers: Invalid argument
Conversion failed!
"""


def test_video_failure_is_not_hidden_behind_a_title():
    """Old behaviour: 'Invalid NAL'/the path/the title was the first 'error',
    classified as not-video - and the CPU retry was refused."""
    log = HEADER + VIDEO_FAILURE
    assert failure_is_video(log) is True
    assert "Failed to sync surface" in first_error_line(log)


def test_audio_failure_is_still_recognised_behind_the_header():
    log = HEADER + AUDIO_FAILURE
    assert failure_is_video(log) is False
    assert "libopus" in first_error_line(log)


def test_title_is_never_reported_as_the_cause():
    log = HEADER + "Conversion failed!\n"
    assert first_error_line(log) == "Conversion failed!"
    assert "Trial and Error" not in first_error_line(log)


def test_header_alone_names_no_culprit():
    """A tail with only the dump in it (e.g. the process was killed) must not
    blame the audio because a track is called 'Failed Dub'."""
    assert failure_is_video(HEADER) is True
    assert "Trial" not in first_error_line(HEADER)


def test_truncated_header_without_stream_mapping():
    """The kept log tail can start in the middle of the dump."""
    tail = "\n".join(HEADER.splitlines()[4:12]) + "\n" + VIDEO_FAILURE
    assert failure_is_video(tail) is True
    assert "Failed to sync surface" in first_error_line(tail)


def test_errors_before_any_input_is_opened_are_kept():
    """Device setup fails before a single dump line is printed."""
    log = (
        "[AVHWDeviceContext @ 0x55] Failed to initialise VAAPI connection: -1 (unknown libva error).\n"
        "Device creation failed: -5.\n"
        "Failed to set value 'vaapi=va:/dev/dri/renderD128' for option 'init_hw_device': "
        "Input/output error\n"
        "Error parsing global options: Input/output error\n"
    )
    assert failure_is_video(log) is True
    assert "VAAPI connection" in first_error_line(log)


def test_input_open_failure_with_an_invalid_path_still_reports_it():
    log = "/media/Invalid/x.mkv: No such file or directory\n"
    assert first_error_line(log) == "/media/Invalid/x.mkv: No such file or directory"
