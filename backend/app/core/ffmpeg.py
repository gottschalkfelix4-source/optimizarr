"""Thin async wrapper around ffmpeg / ffprobe."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Sequence

log = logging.getLogger(__name__)

# jellyfin-ffmpeg ships the Intel QSV/VAAPI stack pre-wired, so prefer it.
FFMPEG_CANDIDATES = [
    "/usr/lib/jellyfin-ffmpeg/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
]
FFPROBE_CANDIDATES = [
    "/usr/lib/jellyfin-ffmpeg/ffprobe",
    "/usr/local/bin/ffprobe",
    "/usr/bin/ffprobe",
]


def _resolve(candidates: Sequence[str], name: str) -> str:
    override = os.environ.get(f"OPTIMIZARR_{name.upper()}")
    if override and Path(override).exists():
        return override
    for c in candidates:
        if Path(c).exists() and os.access(c, os.X_OK):
            return c
    found = shutil.which(name)
    return found or name


FFMPEG = _resolve(FFMPEG_CANDIDATES, "ffmpeg")
FFPROBE = _resolve(FFPROBE_CANDIDATES, "ffprobe")


class FFmpegError(RuntimeError):
    def __init__(self, message: str, returncode: int = -1, log_tail: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.log_tail = log_tail


@dataclass
class Progress:
    """One parsed ``-progress`` block from a running ffmpeg."""

    out_time: float = 0.0        # seconds of source encoded so far
    frame: int = 0
    fps: float = 0.0
    speed: float = 0.0           # x realtime
    total_size: int = 0          # bytes written so far
    bitrate_kbps: float = 0.0
    done: bool = False


@dataclass
class MediaInfo:
    """Normalised ffprobe output."""

    path: str
    container: str = ""
    size: int = 0
    duration: float = 0.0
    overall_bitrate: int = 0
    video_codec: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_bitrate: int = 0
    bit_depth: int = 8
    pix_fmt: str = ""
    is_hdr: bool = False
    hdr_format: str = ""
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""
    interlaced: bool = False
    audio_streams: list[dict[str, Any]] = field(default_factory=list)
    subtitle_streams: list[dict[str, Any]] = field(default_factory=list)
    chapters: int = 0
    attachments: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def pixels_per_second(self) -> float:
        return self.width * self.height * self.fps

    @property
    def bits_per_pixel(self) -> float:
        """Bitrate normalised by resolution and framerate.

        This is the single most useful signal for "is this file bloated?".
        A well-encoded 1080p h264 sits around 0.09, a BluRay remux above 0.30,
        an efficient HEVC around 0.05.
        """
        pps = self.pixels_per_second
        if pps <= 0 or self.video_bitrate <= 0:
            return 0.0
        return self.video_bitrate / pps


#: ffmpeg prints plenty of noise around the line that matters.  These are the
#: shapes that actually explain a failure.
_ERROR_MARKERS = (
    "error", "failed", "unsupported", "invalid", "cannot", "unable",
    "no such", "not implemented", "incompatible", "device creation",
    "impossible to convert",
)
_ERROR_NOISE = ("error_rate", "last message repeated", "deprecated")


#: When one stream fails, ffmpeg tears the whole pipeline down and every other
#: stream reports a follow-on error.  Audio encoders are especially loud about
#: it ("Could not open encoder before EOF"), which makes them look like the
#: cause when they are only a casualty.  These mark a line as consequence.
_CONSEQUENCE_MARKERS = (
    "could not open encoder before eof",
    "error sending frames to consumers",
    "terminating thread with return code",
    "task finished with error code",
    "nothing was written into output file",
    "conversion failed",
    "error closing file",
)


def classify_error_line(line: str) -> str:
    """"video", "other" or "consequence" - which stream a failure belongs to."""
    lowered = line.lower()
    if any(marker in lowered for marker in _CONSEQUENCE_MARKERS):
        return "consequence"
    # ffmpeg tags output streams as vost#/aost#/sost# and decoders as vist#/dec:.
    if any(tag in lowered for tag in ("vost#", "vist#", "[dec:", "hwaccel", "hwupload",
                                      "_qsv", "_vaapi", "libsvtav1", "vf#", "avhwdevice",
                                      "qsv", "vaapi", "device creation")):
        return "video"
    if any(tag in lowered for tag in ("aost#", "af#", "libopus", "audio", "aac", "eac3")):
        return "other"
    if any(tag in lowered for tag in ("sost#", "subtitle")):
        return "other"
    return "other"


def first_error_line(log_tail: str) -> str:
    """The most explanatory line from an ffmpeg failure.

    Two rules, both learned the hard way:

    * Scan **forwards** - ffmpeg names the specific cause first and a vaguer
      summary afterwards, so reading from the end returns "Error while opening
      encoder", which explains nothing.
    * Prefer a line about the **video** stream.  When the video encoder fails,
      every audio encoder in the file reports its own error a moment later; the
      loudest line is usually not the one that started it.
    """
    lines = [ln.strip() for ln in (log_tail or "").splitlines() if ln.strip()]
    candidates: list[tuple[str, str]] = []
    for line in lines:
        lowered = line.lower()
        if any(noise in lowered for noise in _ERROR_NOISE):
            continue
        if any(marker in lowered for marker in _ERROR_MARKERS):
            candidates.append((classify_error_line(line), line))

    for wanted in ("video", "other"):
        for kind, line in candidates:
            if kind == wanted:
                return line[:300]
    if candidates:
        return candidates[0][1][:300]
    return lines[-1][:300] if lines else "keine Fehlermeldung von ffmpeg"


def failure_is_video(log_tail: str) -> bool:
    """Did the video stream cause this failure?

    Retrying on the CPU only helps when the GPU encoder is what broke.  If the
    audio or the muxer failed, the retry burns hours to fail exactly the same
    way, so it is worth being sure before falling back.
    """
    lines = [ln.strip() for ln in (log_tail or "").splitlines() if ln.strip()]
    for line in lines:
        lowered = line.lower()
        if any(noise in lowered for noise in _ERROR_NOISE):
            continue
        if not any(marker in lowered for marker in _ERROR_MARKERS):
            continue
        kind = classify_error_line(line)
        if kind == "video":
            return True
        if kind == "other":
            return False
    # Nothing conclusive: assume it was the video path, since that is the one
    # the caller was about to give up on anyway.
    return True


async def _run(cmd: list[str], timeout: float | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise FFmpegError(f"timeout after {timeout}s: {' '.join(cmd[:4])}")
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _parse_fps(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _bit_depth(stream: dict[str, Any]) -> int:
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        raw = stream.get(key)
        if raw:
            try:
                depth = int(raw)
                if depth > 0:
                    return depth
            except (TypeError, ValueError):
                pass
    pix_fmt = (stream.get("pix_fmt") or "").lower()
    for depth in (12, 10):
        if f"p{depth}" in pix_fmt or f"{depth}le" in pix_fmt or f"{depth}be" in pix_fmt:
            return depth
    return 8


def _detect_hdr(stream: dict[str, Any]) -> tuple[bool, str]:
    transfer = (stream.get("color_transfer") or "").lower()
    side_data = stream.get("side_data_list") or []
    types = {str(sd.get("side_data_type", "")).lower() for sd in side_data}
    if any("dolby vision" in t for t in types):
        return True, "dolby_vision"
    if transfer in ("smpte2084", "smpte st 2084"):
        if any("hdr dynamic metadata" in t for t in types):
            return True, "hdr10plus"
        return True, "hdr10"
    if transfer in ("arib-std-b67", "arib_std_b67"):
        return True, "hlg"
    return False, ""


async def probe(path: str | Path, timeout: float = 120.0) -> MediaInfo:
    """Read metadata for one file."""
    path = str(path)
    cmd = [
        FFPROBE, "-v", "error", "-hide_banner",
        "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters",
        path,
    ]
    code, out, err = await _run(cmd, timeout=timeout)
    if code != 0:
        raise FFmpegError(f"ffprobe failed: {err.strip()[:400]}", code, err[-2000:])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"ffprobe returned invalid JSON: {exc}") from exc

    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    info = MediaInfo(path=path, raw=data)
    info.container = (fmt.get("format_name") or "").split(",")[0]
    try:
        info.size = int(fmt.get("size") or 0)
    except (TypeError, ValueError):
        info.size = 0
    if not info.size:
        try:
            info.size = os.path.getsize(path)
        except OSError:
            pass
    info.duration = float(fmt.get("duration") or 0.0)
    try:
        info.overall_bitrate = int(fmt.get("bit_rate") or 0)
    except (TypeError, ValueError):
        info.overall_bitrate = 0
    info.chapters = len(data.get("chapters") or [])

    video = None
    for s in streams:
        codec_type = s.get("codec_type")
        if codec_type == "video":
            # Skip cover art / thumbnails masquerading as video streams.
            disposition = s.get("disposition") or {}
            if disposition.get("attached_pic") or s.get("codec_name") in ("mjpeg", "png", "bmp", "gif"):
                continue
            if video is None:
                video = s
        elif codec_type == "audio":
            tags = s.get("tags") or {}
            disposition = s.get("disposition") or {}
            info.audio_streams.append({
                "index": s.get("index"),
                "codec": s.get("codec_name", ""),
                "channels": s.get("channels", 2),
                "channel_layout": s.get("channel_layout", ""),
                "bitrate": int(s.get("bit_rate") or 0),
                "sample_rate": int(s.get("sample_rate") or 0),
                "language": (tags.get("language") or "und").lower(),
                "title": tags.get("title", ""),
                "default": bool(disposition.get("default")),
                "commentary": bool(disposition.get("comment"))
                or "commentar" in (tags.get("title", "").lower()),
            })
        elif codec_type == "subtitle":
            tags = s.get("tags") or {}
            disposition = s.get("disposition") or {}
            info.subtitle_streams.append({
                "index": s.get("index"),
                "codec": s.get("codec_name", ""),
                "language": (tags.get("language") or "und").lower(),
                "title": tags.get("title", ""),
                "forced": bool(disposition.get("forced")),
                "default": bool(disposition.get("default")),
                "text": s.get("codec_name") in ("subrip", "ass", "ssa", "mov_text", "webvtt", "text"),
            })
        elif codec_type == "attachment":
            info.attachments += 1

    if video is None:
        raise FFmpegError("no usable video stream")

    info.video_codec = (video.get("codec_name") or "").lower()
    info.profile = video.get("profile") or ""
    info.width = int(video.get("width") or 0)
    info.height = int(video.get("height") or 0)
    info.pix_fmt = video.get("pix_fmt") or ""
    info.bit_depth = _bit_depth(video)
    info.color_primaries = video.get("color_primaries") or ""
    info.color_transfer = video.get("color_transfer") or ""
    info.color_space = video.get("color_space") or ""
    info.is_hdr, info.hdr_format = _detect_hdr(video)
    info.interlaced = (video.get("field_order") or "progressive") not in ("progressive", "unknown", "")

    info.fps = _parse_fps(video.get("avg_frame_rate")) or _parse_fps(video.get("r_frame_rate"))
    if info.fps > 1000 or info.fps <= 0:
        info.fps = _parse_fps(video.get("r_frame_rate")) or 24.0

    if not info.duration:
        info.duration = float((video.get("tags") or {}).get("DURATION-eng", 0) or 0) or 0.0

    try:
        info.video_bitrate = int(video.get("bit_rate") or 0)
    except (TypeError, ValueError):
        info.video_bitrate = 0
    if not info.video_bitrate:
        # Many MKVs carry no per-stream bitrate: derive it from the container
        # total minus a conservative estimate of the audio tracks.
        audio_bits = 0
        for a in info.audio_streams:
            audio_bits += a["bitrate"] or (a["channels"] * 64_000)
        sub_overhead = len(info.subtitle_streams) * 2_000
        if info.overall_bitrate:
            info.video_bitrate = max(0, info.overall_bitrate - audio_bits - sub_overhead)
        elif info.duration > 0 and info.size:
            total = int(info.size * 8 / info.duration)
            info.video_bitrate = max(0, total - audio_bits - sub_overhead)

    return info


_PROGRESS_KEYS = {
    "frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms", "speed", "progress",
}


async def run_with_progress(
    args: list[str],
    on_progress: Callable[[Progress], Any] | None = None,
    log_lines: int = 400,
    timeout: float | None = None,
    cancel_event: asyncio.Event | None = None,
    nice: int = 0,
) -> tuple[int, str]:
    """Run ffmpeg, streaming ``-progress`` updates to ``on_progress``.

    Returns (returncode, tail of stderr).  Kills the process if ``cancel_event``
    fires or ``timeout`` elapses.
    """
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-progress", "pipe:1", "-nostats", *args]
    if nice and hasattr(os, "nice"):
        cmd = ["nice", "-n", str(nice), *cmd]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    tail: list[str] = []
    progress = Progress()

    async def pump_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                tail.append(text)
                if len(tail) > log_lines:
                    del tail[0 : len(tail) - log_lines]

    async def pump_stdout() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if "=" not in text:
                continue
            key, _, value = text.partition("=")
            if key not in _PROGRESS_KEYS:
                continue
            try:
                if key == "frame":
                    progress.frame = int(value)
                elif key == "fps":
                    progress.fps = float(value)
                elif key == "total_size":
                    progress.total_size = int(value)
                elif key == "out_time_us":
                    progress.out_time = int(value) / 1_000_000
                elif key == "out_time_ms":
                    # ffmpeg reports out_time_ms in microseconds despite the name
                    progress.out_time = int(value) / 1_000_000
                elif key == "bitrate":
                    progress.bitrate_kbps = float(value.replace("kbits/s", "").strip() or 0)
                elif key == "speed":
                    progress.speed = float(value.replace("x", "").strip() or 0)
                elif key == "progress":
                    progress.done = value == "end"
                    if on_progress:
                        res = on_progress(progress)
                        if asyncio.iscoroutine(res):
                            await res
            except (ValueError, TypeError):
                continue

    async def watch_cancel() -> None:
        if cancel_event is None:
            await asyncio.Future()  # never resolves
        assert cancel_event is not None
        await cancel_event.wait()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()

    tasks = [
        asyncio.create_task(pump_stdout()),
        asyncio.create_task(pump_stderr()),
    ]
    cancel_task = asyncio.create_task(watch_cancel())
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise FFmpegError(f"encode exceeded {timeout}s", -9, "\n".join(tail[-30:]))
    finally:
        cancel_task.cancel()
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                t.cancel()

    return proc.returncode or 0, "\n".join(tail)


async def run_simple(args: list[str], timeout: float | None = 900.0) -> tuple[int, str, str]:
    """Run ffmpeg and wait, no progress parsing."""
    return await _run([FFMPEG, "-hide_banner", "-nostdin", *args], timeout=timeout)


_encoder_cache: set[str] | None = None
_filter_cache: set[str] | None = None


async def available_encoders(refresh: bool = False) -> set[str]:
    global _encoder_cache
    if _encoder_cache is not None and not refresh:
        return _encoder_cache
    code, out, _ = await _run([FFMPEG, "-hide_banner", "-encoders"], timeout=30)
    names: set[str] = set()
    if code == 0:
        for line in out.splitlines():
            m = re.match(r"^\s*[A-Z.]{6}\s+(\S+)", line)
            if m:
                names.add(m.group(1))
    _encoder_cache = names
    return names


async def available_filters(refresh: bool = False) -> set[str]:
    global _filter_cache
    if _filter_cache is not None and not refresh:
        return _filter_cache
    code, out, _ = await _run([FFMPEG, "-hide_banner", "-filters"], timeout=30)
    names: set[str] = set()
    if code == 0:
        for line in out.splitlines():
            m = re.match(r"^\s*[TSC.]{3}\s+(\S+)", line)
            if m:
                names.add(m.group(1))
    _filter_cache = names
    return names


async def version() -> str:
    code, out, _ = await _run([FFMPEG, "-version"], timeout=15)
    if code != 0 or not out:
        return "unknown"
    return out.splitlines()[0].strip()


async def extract_segment(
    source: str, start: float, duration: float, dest: str, timeout: float = 300.0
) -> None:
    """Cut a lossless slice used for trial encodes and VMAF probes."""
    args = [
        "-y", "-ss", f"{start:.3f}", "-i", source, "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-c:v", "copy", "-an", "-sn", "-dn",
        "-avoid_negative_ts", "make_zero", "-f", "matroska", dest,
    ]
    code, _, err = await run_simple(args, timeout=timeout)
    if code != 0 or not os.path.exists(dest) or os.path.getsize(dest) < 1024:
        # Stream copy can land between keyframes: re-cut by decoding instead.
        args = [
            "-y", "-ss", f"{start:.3f}", "-i", source, "-t", f"{duration:.3f}",
            "-map", "0:v:0", "-c:v", "ffv1", "-level", "3", "-an", "-sn", "-dn",
            "-f", "matroska", dest,
        ]
        code, _, err = await run_simple(args, timeout=timeout)
        if code != 0:
            raise FFmpegError(f"segment extraction failed: {err.strip()[-300:]}", code, err[-1500:])
