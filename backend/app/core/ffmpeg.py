"""Thin async wrapper around ffmpeg / ffprobe."""
from __future__ import annotations

import asyncio
import json
import logging
import math
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


_DUMP_HEADER = re.compile(r"^(Input|Output) #\d+[,:]")


def _diagnostic_lines(log_tail: str) -> list[str]:
    """The lines of an ffmpeg log that can explain a failure.

    Before it does any work ffmpeg describes its inputs and outputs: file
    paths, container tags, every stream with its title.  None of that is a
    diagnosis, but it reads like one to a keyword search - an episode called
    "Trial and Error", a folder named "Invalid", a track titled "Audio failed
    take" all used to be reported as the cause and decided whether the GPU got
    blamed.  So:

    * everything up to ``Stream mapping:`` is setup and dropped - an error that
      stops ffmpeg earlier ends the log before that line is ever printed;
    * the ``Input #``/``Output #`` headers are dropped, and with them every
      indented line, which is how ffmpeg prints the body of those dumps (and
      of the stream mapping).  Its diagnostics are never indented.
    """
    raw = (log_tail or "").splitlines()
    for i, line in enumerate(raw):
        if line.strip() == "Stream mapping:":
            raw = raw[i + 1:]
            break
    lines: list[str] = []
    for line in raw:
        if not line.strip() or line[:1].isspace() or _DUMP_HEADER.match(line):
            continue
        lines.append(line.strip())
    return lines


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
    lines = _diagnostic_lines(log_tail)
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
    for line in _diagnostic_lines(log_tail):
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


class FFmpegCancelled(FFmpegError):
    """The caller's cancel event fired and the process was stopped."""


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL and reap.  Safe to call on a process that already exited.

    Used on every way out that is not a normal exit - a timeout, a cancelled
    task, an exception in the caller.  An encode that outlives the task that
    started it keeps a GPU session and several CPU cores busy for hours, and
    nothing is left that would ever stop it.
    """
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await proc.wait()
    except Exception:  # pragma: no cover - reaping must never mask the real error
        log.debug("could not reap ffmpeg process %s", proc.pid, exc_info=True)


async def _terminate(proc: asyncio.subprocess.Process, grace: float = 10.0) -> None:
    """SIGTERM, give ffmpeg ``grace`` seconds to finish up, then SIGKILL."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        await _kill(proc)


async def _run(
    cmd: list[str], timeout: float | None = None, cancel_event: asyncio.Event | None = None
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    communicate = asyncio.ensure_future(proc.communicate())
    cancelled = (
        asyncio.ensure_future(cancel_event.wait()) if cancel_event is not None else None
    )
    try:
        waiting = {communicate} if cancelled is None else {communicate, cancelled}
        done, _ = await asyncio.wait(
            waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if communicate in done:
            out, err = communicate.result()
            return (proc.returncode or 0, out.decode("utf-8", "replace"),
                    err.decode("utf-8", "replace"))
        await _kill(proc)
        if cancelled is not None and cancelled in done:
            raise FFmpegCancelled(f"abgebrochen: {' '.join(cmd[:4])}", -9)
        raise FFmpegError(f"timeout after {timeout}s: {' '.join(cmd[:4])}")
    except BaseException:
        # CancelledError included: the task that owns this process is gone, so
        # the process has to go too.
        await _kill(proc)
        raise
    finally:
        if cancelled is not None:
            cancelled.cancel()
        if not communicate.done():
            # The pipes close once the process is dead; let communicate() see
            # EOF rather than leaving a pending task behind.
            try:
                await asyncio.wait({communicate}, timeout=5)
            except BaseException:  # pragma: no cover
                pass
            if not communicate.done():
                communicate.cancel()


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


def _to_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def _to_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_duration_tag(value: Any) -> float:
    """Seconds from a Matroska ``DURATION`` tag, 0.0 if it cannot be read.

    mkvmerge writes ``01:23:45.000000000`` - hours, minutes, and seconds with
    nanosecond precision - which ``float()`` rejects.  Plain seconds are
    accepted too.  A broken tag must never make a file unreadable: the
    duration is a convenience, and the probe falls back to 0.
    """
    text = str(value or "").strip()
    if not text:
        return 0.0
    if ":" not in text:
        return _to_float(text)
    parts = text.split(":")
    if len(parts) > 3:
        return 0.0
    total = 0.0
    try:
        for position, part in enumerate(parts[:-1]):
            if not part.strip().isdigit():
                return 0.0
            if position > 0 and int(part) >= 60:  # minutes after hours
                return 0.0
            total = total * 60 + int(part)
        seconds = float(parts[-1])
    except ValueError:
        return 0.0
    if not math.isfinite(seconds) or seconds < 0 or seconds >= 60:
        return 0.0
    return total * 60 + seconds


def _duration_from_tags(tags: dict[str, Any] | None) -> float:
    """``DURATION`` or a language-suffixed ``DURATION-eng``, whichever parses."""
    for key, value in (tags or {}).items():
        if str(key).upper().split("-", 1)[0] == "DURATION":
            seconds = parse_duration_tag(value)
            if seconds > 0:
                return seconds
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


DOLBY_VISION = "dolby_vision"


def _dolby_vision_profile(stream: dict[str, Any]) -> int | None:
    """DV profile from the DOVI configuration record; 0 = DV of unknown profile.

    ffprobe names the side data "DOVI configuration record" - older code looked
    for "dolby vision" and never matched, so profile 5 (no HDR10 fallback, IPT
    colour) was treated as SDR and came out green/purple.
    """
    for sd in stream.get("side_data_list") or []:
        kind = str(sd.get("side_data_type", "")).lower()
        if "dovi" in kind or "dolby vision" in kind:
            try:
                return int(sd.get("dv_profile") or 0)
            except (TypeError, ValueError):
                return 0
    tag = str(stream.get("codec_tag_string") or "").lower()
    if tag in ("dvh1", "dvhe", "dav1", "dva1", "dvav"):
        return 0
    return None


def is_dolby_vision(hdr_format: str | None) -> bool:
    return bool(hdr_format) and str(hdr_format).startswith(DOLBY_VISION)


def dolby_vision_profile(hdr_format: str | None) -> int | None:
    """Profile encoded in ``hdr_format`` ("dolby_vision_p8" -> 8), 0 if unknown,
    None if the file is not Dolby Vision at all."""
    if not is_dolby_vision(hdr_format):
        return None
    m = re.search(r"_p(\d+)$", str(hdr_format))
    return int(m.group(1)) if m else 0


def _detect_hdr(stream: dict[str, Any]) -> tuple[bool, str]:
    """(is_hdr, hdr_format).  Dolby Vision is reported as ``dolby_vision_p<N>``."""
    transfer = (stream.get("color_transfer") or "").lower()
    side_data = stream.get("side_data_list") or []
    types = {str(sd.get("side_data_type", "")).lower() for sd in side_data}
    dv = _dolby_vision_profile(stream)
    if dv is not None:
        return True, f"{DOLBY_VISION}_p{dv}" if dv else DOLBY_VISION
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
    info.duration = _to_float(fmt.get("duration"))
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
                "bitrate": _to_int(s.get("bit_rate")),
                "sample_rate": _to_int(s.get("sample_rate")),
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
        info.duration = _to_float(video.get("duration")) or _duration_from_tags(video.get("tags"))

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


#: A pipe line longer than this is cut, not buffered whole.  asyncio's own
#: ``readline`` gives up at 64 KiB with an exception - which used to end the
#: reader, leave the pipe unread and stall ffmpeg as soon as it filled.
_MAX_LINE = 64 * 1024
#: What a single stderr line may occupy in the kept log tail.
_MAX_LOG_LINE = 4000


async def _read_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Yield lines until EOF, whatever their length.

    A line longer than the stream buffer comes out in pieces instead of
    raising; the caller only ever sees bytes.
    """
    while True:
        try:
            line = await stream.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                yield exc.partial
            return
        except asyncio.LimitOverrunError as exc:
            # No newline within the buffer: hand out what is there as one
            # piece and carry on - the remainder of the line follows next.
            yield await stream.readexactly(max(1, exc.consumed))
            continue
        yield line


async def _drain(stream: asyncio.StreamReader) -> None:
    """Read and discard until EOF, so the writer can never block on this pipe."""
    while True:
        try:
            chunk = await stream.read(_MAX_LINE)
        except Exception:  # pragma: no cover - a broken pipe is as good as EOF
            return
        if not chunk:
            return


def _parse_progress_line(progress: Progress, text: str) -> str | None:
    """Fold one ``key=value`` line into ``progress``; returns the key it set."""
    key, sep, value = text.partition("=")
    if not sep or key not in _PROGRESS_KEYS:
        return None
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
    except (ValueError, TypeError):
        # "N/A" and friends - keep the previous value.
        return None if key != "progress" else key
    return key


async def run_with_progress(
    args: list[str],
    on_progress: Callable[[Progress], Any] | None = None,
    log_lines: int = 400,
    timeout: float | None = None,
    cancel_event: asyncio.Event | None = None,
    nice: int = 0,
) -> tuple[int, str]:
    """Run ffmpeg, streaming ``-progress`` updates to ``on_progress``.

    Returns (returncode, tail of stderr).  Stops the process if ``cancel_event``
    fires (SIGTERM, SIGKILL after 10 s) or ``timeout`` elapses, and kills it
    outright if the calling task is cancelled or anything else goes wrong - an
    ffmpeg must never outlive the call that started it.

    Both pipes are read until EOF no matter what: a failing progress callback or
    an absurdly long log line is logged and skipped, because a pipe nobody reads
    fills up and freezes ffmpeg mid-encode.
    """
    # -stats_period sets how often ffmpeg emits a -progress block.  Left to the
    # default it is coarse enough that a progress bar visibly steps rather than
    # moves, so it is pinned here instead of inherited.
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin",
        "-progress", "pipe:1", "-nostats", "-stats_period", "0.4",
        *args,
    ]
    if nice and hasattr(os, "nice"):
        cmd = ["nice", "-n", str(nice), *cmd]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    tail: list[str] = []
    progress = Progress()
    callback_errors = 0

    async def pump_stderr() -> None:
        assert proc.stderr is not None
        try:
            async for line in _read_lines(proc.stderr):
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    tail.append(text[:_MAX_LOG_LINE])
                    if len(tail) > log_lines:
                        del tail[0 : len(tail) - log_lines]
        except Exception:
            log.exception("reading ffmpeg's stderr failed - discarding the rest")
            await _drain(proc.stderr)

    async def pump_stdout() -> None:
        nonlocal callback_errors
        assert proc.stdout is not None
        try:
            async for line in _read_lines(proc.stdout):
                text = line.decode("utf-8", "replace").strip()
                if _parse_progress_line(progress, text) != "progress" or not on_progress:
                    continue
                try:
                    res = on_progress(progress)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    # The encode is fine; only the reporting broke.  Say so
                    # once (and then now and then), and keep reading.
                    callback_errors += 1
                    if callback_errors == 1 or callback_errors % 500 == 0:
                        log.exception("progress callback failed (%d times so far)", callback_errors)
        except Exception:
            log.exception("reading ffmpeg's progress failed - discarding the rest")
            await _drain(proc.stdout)

    async def watch_cancel() -> None:
        assert cancel_event is not None
        await cancel_event.wait()
        await _terminate(proc, grace=10)

    pumps = [
        asyncio.create_task(pump_stdout()),
        asyncio.create_task(pump_stderr()),
    ]
    cancel_task = asyncio.create_task(watch_cancel()) if cancel_event is not None else None
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        await _kill(proc)
        raise FFmpegError(f"encode exceeded {timeout}s", -9, "\n".join(tail[-30:]))
    except BaseException:
        # Task cancelled (worker shutdown, job aborted from outside) or an
        # unexpected error: the process is killed here rather than orphaned,
        # and this also completes a SIGTERM escalation that was in progress.
        await _kill(proc)
        raise
    finally:
        if proc.returncode is None:  # pragma: no cover - every path above reaps
            await _kill(proc)
        if cancel_task is not None:
            # The process has exited, so there is nothing left to escalate.
            cancel_task.cancel()
        done, pending = await asyncio.wait(pumps, timeout=5)
        for task in pending:
            task.cancel()
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.warning("ffmpeg output reader failed: %r", task.exception())

    return proc.returncode or 0, "\n".join(tail)


async def run_simple(
    args: list[str], timeout: float | None = 900.0, cancel_event: asyncio.Event | None = None,
) -> tuple[int, str, str]:
    """Run ffmpeg and wait, no progress parsing.

    ``cancel_event`` stops the process early; that raises
    :class:`FFmpegCancelled` (an :class:`FFmpegError`).
    """
    return await _run(
        [FFMPEG, "-hide_banner", "-nostdin", *args], timeout=timeout, cancel_event=cancel_event
    )


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
            # Three flag columns (timeline, slice threads, commands) up to
            # ffmpeg 7; newer builds dropped the command column.  Matching
            # only three found no filter at all there - not even ssim.
            m = re.match(r"^\s*[TSC.]{2,3}\s+(\S+)", line)
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
    source: str, start: float, duration: float, dest: str, timeout: float = 300.0,
    exact: bool = False, cancel_event: asyncio.Event | None = None,
) -> None:
    """Cut a lossless slice used for trial encodes and VMAF probes.

    ``exact`` decodes instead of stream-copying.  A copied slice starts at the
    keyframe before ``start``, and two files with different keyframe grids -
    a source and its encode - then yield slices that do not line up, which
    scores a good encode as a bad one.

    ``cancel_event`` stops the cut early (raises :class:`FFmpegCancelled`).
    """
    code, err = 1, ""
    args = [
        "-y", "-ss", f"{start:.3f}", "-i", source, "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-c:v", "copy", "-an", "-sn", "-dn",
        "-avoid_negative_ts", "make_zero", "-f", "matroska", dest,
    ]
    if not exact:
        code, _, err = await run_simple(args, timeout=timeout, cancel_event=cancel_event)
    if code != 0 or not os.path.exists(dest) or os.path.getsize(dest) < 1024:
        # Stream copy can land between keyframes: re-cut by decoding instead.
        args = [
            "-y", "-ss", f"{start:.3f}", "-i", source, "-t", f"{duration:.3f}",
            "-map", "0:v:0", "-c:v", "ffv1", "-level", "3", "-an", "-sn", "-dn",
            "-f", "matroska", dest,
        ]
        code, _, err = await run_simple(args, timeout=timeout, cancel_event=cancel_event)
        if code != 0:
            raise FFmpegError(f"segment extraction failed: {err.strip()[-300:]}", code, err[-1500:])
