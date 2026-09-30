"""Objective quality measurement, plus a film-grain estimator.

**On the choice of metric.**  VMAF is the metric everyone quotes, but neither
Debian's ffmpeg nor jellyfin-ffmpeg is built with ``libvmaf`` - and jellyfin's
build is the one that makes Intel QSV work properly, which matters more here.
Pulling in a second 250 MB static ffmpeg just for one filter is not a good
trade for a home server image.

So Optimizarr measures with whatever the running ffmpeg actually has:

* ``libvmaf`` when the build provides it (nothing to configure, it is detected),
* otherwise **SSIM**, which every ffmpeg build has.

Both are used the same way - encode a segment, compare it against the source,
and move CRF until the score hits the target - and for that job what matters is
that the score falls monotonically as CRF rises, which both metrics do.  Scores
are reported in their own scale plus a clearly-labelled VMAF *estimate*, so the
familiar "94 is visually transparent" rule of thumb still works.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import ffmpeg

if TYPE_CHECKING:  # pragma: no cover
    from .planner import EncodePlan

log = logging.getLogger(__name__)

_PSNR_RE = re.compile(r"average:([0-9.]+)")
_SSIM_RE = re.compile(r"All:\s*([0-9.]+)")

# Anchor points mapping SSIM onto the VMAF scale.  These are rough empirical
# equivalences for typical live-action material at 1080p and above; they exist
# so the UI can keep speaking in VMAF terms, not to claim SSIM and VMAF are
# interchangeable.  Interpolated linearly between anchors.
_SSIM_VMAF_ANCHORS: list[tuple[float, float]] = [
    (0.880, 70.0),
    (0.925, 80.0),
    (0.945, 85.0),
    (0.968, 91.0),
    (0.980, 94.0),
    (0.988, 96.0),
    (0.994, 98.0),
    (1.000, 100.0),
]


@dataclass
class QualityScore:
    """One measurement, in whichever metric was available."""

    value: float                  # raw score in the native metric
    metric: str                   # "vmaf" or "ssim"
    vmaf_estimate: float          # always on the 0..100 VMAF-like scale
    successful: int = 1
    planned: int = 1
    worst_vmaf: float | None = None

    @property
    def is_exact(self) -> bool:
        return self.metric == "vmaf"

    def describe(self) -> str:
        if self.is_exact:
            return f"VMAF {self.value:.1f}"
        return f"SSIM {self.value:.4f} (entspricht etwa VMAF {self.vmaf_estimate:.0f})"


def ssim_to_vmaf(ssim: float) -> float:
    """Map an SSIM score onto the VMAF scale (approximate, see module docs)."""
    ssim = max(0.0, min(1.0, ssim))
    if ssim <= _SSIM_VMAF_ANCHORS[0][0]:
        # Below the lowest anchor, fall off steeply but stay in range.
        return max(0.0, 70.0 - (_SSIM_VMAF_ANCHORS[0][0] - ssim) * 400.0)
    for (s0, v0), (s1, v1) in zip(_SSIM_VMAF_ANCHORS, _SSIM_VMAF_ANCHORS[1:]):
        if s0 <= ssim <= s1:
            t = (ssim - s0) / (s1 - s0) if s1 > s0 else 0.0
            return v0 + t * (v1 - v0)
    return 100.0


def vmaf_to_ssim(vmaf: float) -> float:
    """Inverse of :func:`ssim_to_vmaf` - turns a VMAF target into an SSIM one."""
    vmaf = max(0.0, min(100.0, vmaf))
    if vmaf <= _SSIM_VMAF_ANCHORS[0][1]:
        return _SSIM_VMAF_ANCHORS[0][0]
    for (s0, v0), (s1, v1) in zip(_SSIM_VMAF_ANCHORS, _SSIM_VMAF_ANCHORS[1:]):
        if v0 <= vmaf <= v1:
            t = (vmaf - v0) / (v1 - v0) if v1 > v0 else 0.0
            return s0 + t * (s1 - s0)
    return 1.0


async def available_metric() -> str:
    """Which comparison metric this ffmpeg build can provide."""
    filters = await ffmpeg.available_filters()
    if "libvmaf" in filters:
        return "vmaf"
    if "ssim" in filters:
        return "ssim"
    return "none"


#: Common pixel format both inputs are brought to before comparing.  libvmaf
#: and ssim want identical formats on both pads; a 10-bit encode of an 8-bit
#: source otherwise fails the graph (or, worse, gets auto-converted on one pad
#: only).  Widening 8-bit to 10-bit is lossless, so nothing is judged unfairly.
COMPARE_PIX_FMT = "yuv420p10le"

#: Pairs frames by their index instead of their timestamp.  Both slices hold
#: the same pictures in the same order, but their timestamps need not agree:
#: Matroska stores milliseconds, and at 24000/1001 fps a source muxed from a
#: 90 kHz clock rounds some frames one millisecond the other way than its
#: encode does.  ``PTS-STARTPTS`` kept that jitter, and the metric then
#: compared a frame against its predecessor about once a second - a clean HDR
#: encode measured VMAF 78 instead of 92.5.  ``N/FRAME_RATE/TB`` is not enough
#: either: it still rounds into each input's own timebase (ms against 90 kHz
#: for an MP4 trial encode) and breaks when the frame rate is unknown.  A
#: shared timebase and the bare frame number are exact on both pads.
FRAME_INDEX_PTS = "settb=AVTB,setpts=N"


def build_compare_graph(metric_filter: str, width: int = 0, height: int = 0) -> str:
    """Filter graph comparing input 0 (distorted) against input 1 (reference).

    Both pads are named and prepared explicitly.  The old graph scaled the
    encode with ``scale=rw:rh`` - variables the plain scale filter does not
    have - so every measurement of a downscaled encode failed and the quality
    gate quietly let it through.  The encode is instead scaled to the known
    reference size taken from the probe.  Frames are paired by index, see
    :data:`FRAME_INDEX_PTS`.
    """
    dist = [FRAME_INDEX_PTS]
    if width > 0 and height > 0:
        # Judge a downscaled encode on the canvas the viewer actually sees.
        dist.append(f"scale={int(width)}:{int(height)}:flags=bicubic")
    dist.append(f"format={COMPARE_PIX_FMT}")
    ref = [FRAME_INDEX_PTS, f"format={COMPARE_PIX_FMT}"]
    return (
        f"[0:v:0]{','.join(dist)}[dist];"
        f"[1:v:0]{','.join(ref)}[ref];"
        f"[dist][ref]{metric_filter}"
    )


async def _measure_vmaf(
    reference: str, distorted: str, threads: int, width: int, height: int, timeout: float,
    cancel_event: asyncio.Event | None = None,
) -> float | None:
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    log_path = tmp.name
    # ffmpeg's filtergraph parser treats ':' and '\' as separators.
    escaped = log_path.replace("\\", "/").replace(":", r"\:")
    graph = build_compare_graph(
        f"libvmaf=log_fmt=json:log_path={escaped}:n_threads={max(1, threads)}", width, height
    )

    try:
        code, _, err = await ffmpeg.run_simple(
            ["-i", distorted, "-i", reference, "-lavfi", graph, "-f", "null", "-"],
            timeout=timeout, cancel_event=cancel_event,
        )
        if code != 0:
            log.warning("VMAF run failed: %s", err.strip()[-300:])
            return None
        with open(log_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        pooled = (data.get("pooled_metrics") or {}).get("vmaf") or {}
        mean = pooled.get("mean")
        if mean is None:
            scores = [
                f.get("metrics", {}).get("vmaf")
                for f in data.get("frames") or []
            ]
            scores = [s for s in scores if isinstance(s, (int, float))]
            mean = sum(scores) / len(scores) if scores else None
        return float(mean) if mean is not None else None
    except ffmpeg.FFmpegCancelled:
        # Stopped on purpose - not a failed measurement worth an SSIM retry.
        raise
    except (OSError, json.JSONDecodeError, ffmpeg.FFmpegError) as exc:
        log.warning("VMAF measurement failed: %s", exc)
        return None
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass


async def _measure_ssim(
    reference: str, distorted: str, width: int, height: int, timeout: float,
    cancel_event: asyncio.Event | None = None,
) -> float | None:
    graph = build_compare_graph("ssim", width, height)
    try:
        code, _, err = await ffmpeg.run_simple(
            ["-i", distorted, "-i", reference, "-lavfi", graph, "-f", "null", "-"],
            timeout=timeout, cancel_event=cancel_event,
        )
    except ffmpeg.FFmpegCancelled:
        raise
    except ffmpeg.FFmpegError as exc:
        log.warning("SSIM measurement failed: %s", exc)
        return None
    if code != 0:
        log.warning("SSIM run failed: %s", err.strip()[-300:])
        return None
    match = _SSIM_RE.search(err)
    if not match:
        log.warning("SSIM run printed no score: %s", err.strip()[-300:])
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


async def measure_quality(
    reference: str,
    distorted: str,
    threads: int = 4,
    scale_to_reference: bool = True,
    timeout: float = 1800.0,
    width: int = 0,
    height: int = 0,
    cancel_event: asyncio.Event | None = None,
) -> QualityScore | None:
    """Compare ``distorted`` against ``reference``.

    ``width``/``height`` are the reference's picture size; the encode is scaled
    to it.  Left at 0 with ``scale_to_reference`` the reference is probed for
    it.  Returns ``None`` when nothing could be measured - callers that gate on
    quality must treat that as "unknown", not as "fine".

    ``cancel_event`` stops the running comparison; that raises
    :class:`ffmpeg.FFmpegCancelled` instead of returning ``None``.
    """
    if scale_to_reference and not (width > 0 and height > 0):
        try:
            ref_info = await ffmpeg.probe(reference)
            width, height = ref_info.width, ref_info.height
        except ffmpeg.FFmpegError as exc:
            log.warning("could not read the reference size of %s: %s", reference, exc)
            width = height = 0
    if not scale_to_reference:
        width = height = 0

    metric = await available_metric()
    if metric == "vmaf":
        score = await _measure_vmaf(
            reference, distorted, threads, width, height, timeout, cancel_event
        )
        if score is not None:
            return QualityScore(value=score, metric="vmaf", vmaf_estimate=score)
        # A failed VMAF run is worth retrying as SSIM rather than giving up.
        metric = "ssim"
    if metric == "ssim":
        ssim = await _measure_ssim(reference, distorted, width, height, timeout, cancel_event)
        if ssim is not None:
            return QualityScore(value=ssim, metric="ssim", vmaf_estimate=ssim_to_vmaf(ssim))
        return None
    log.warning("no quality metric available in this ffmpeg build")
    return None


async def measure_grain(
    sample_path: str, timeout: float = 300.0, cancel_event: asyncio.Event | None = None,
) -> float:
    """Estimate how much film grain / sensor noise a clip carries (0..1).

    Denoise the clip and compare it against itself: the more the denoiser
    changes, the more high-frequency noise there was.  Grainy sources are the
    classic case where naive AV1 settings *grow* a file, and the classic case
    where grain synthesis wins big - so it is worth measuring rather than
    guessing.

    ``cancel_event`` stops the probe (raises :class:`ffmpeg.FFmpegCancelled`).
    """
    graph = "split[a][b];[a]hqdn3d=4:4:9:9[den];[b][den]psnr"
    try:
        code, _, err = await ffmpeg.run_simple(
            ["-i", sample_path, "-lavfi", graph, "-f", "null", "-"],
            timeout=timeout, cancel_event=cancel_event,
        )
    except ffmpeg.FFmpegCancelled:
        raise
    except ffmpeg.FFmpegError as exc:
        log.debug("grain probe failed: %s", exc)
        return 0.0
    if code != 0:
        return 0.0
    match = _PSNR_RE.search(err)
    if not match:
        return 0.0
    try:
        psnr = float(match.group(1))
    except ValueError:
        return 0.0
    if psnr <= 0 or psnr > 100:
        return 0.0
    # 42 dB and above: clean digital source.  30 dB: heavy grain.
    return max(0.0, min(1.0, (42.0 - psnr) / 12.0))


def grain_synthesis_level(grain: float, is_hdr: bool = False) -> int:
    """Map a measured grain level onto an SVT-AV1 ``film-grain`` value.

    Grain synthesis throws the noise away before encoding and re-generates it on
    playback.  That is a huge bitrate win on grainy sources, but it visibly
    smears clean ones, so stay at 0 unless there is real grain to remove.
    """
    if grain < 0.22:
        return 0
    scaled = int(round(4 + (grain - 0.22) * 34))
    if is_hdr:
        scaled = int(scaled * 0.8)
    return max(4, min(28, scaled))


ANIMATION_HINTS = (
    "anime", "animation", "cartoon", "toons", "ghibli", "pixar", "dreamworks",
    "ova", "shounen",
)


def looks_like_animation(path: str) -> bool:
    """Cheap filename heuristic; the advisor refines it when enabled."""
    lowered = str(path).lower()
    return any(hint in lowered for hint in ANIMATION_HINTS)


def _parse_clock(value: str) -> float:
    """``"01:23:45.678000000"`` (Matroska's per-stream DURATION tag) -> seconds."""
    try:
        parts = [float(p) for p in str(value).strip().split(":")]
    except ValueError:
        return 0.0
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def video_stream_duration(info: ffmpeg.MediaInfo) -> float:
    """Length of the main video stream itself, 0 when the file does not say.

    The container duration is the longest stream: an encode whose picture died
    after ten minutes still reports the full length through its audio.
    """
    for stream in (info.raw or {}).get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic"):
            continue
        try:
            value = float(stream.get("duration") or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
        for key, raw in (stream.get("tags") or {}).items():
            if str(key).upper().startswith("DURATION"):
                value = _parse_clock(raw)
                if value > 0:
                    return value
        return 0.0
    return 0.0


def _kept(actions: list[dict] | None) -> int:
    return sum(1 for a in actions or [] if a.get("action") != "drop")


async def verify_output(
    source_info: ffmpeg.MediaInfo,
    output_path: str,
    max_duration_drift: float = 2.0,
    plan: "EncodePlan | None" = None,
) -> tuple[bool, str]:
    """Sanity-check a finished encode before it is allowed to replace anything."""
    try:
        out = await ffmpeg.probe(output_path)
    except ffmpeg.FFmpegError as exc:
        return False, f"Ergebnisdatei nicht lesbar: {exc}"

    if out.video_codec != "av1":
        return False, f"Ergebnis enthaelt {out.video_codec or 'kein'} Video statt AV1"
    if out.duration <= 0:
        return False, "Ergebnisdatei hat keine Laufzeit"
    drift = abs(out.duration - source_info.duration)
    if source_info.duration > 0 and drift > max_duration_drift:
        return False, (
            f"Laufzeit weicht um {drift:.1f}s ab "
            f"({source_info.duration:.1f}s -> {out.duration:.1f}s)"
        )
    # The video stream on its own: a picture that stops early hides behind the
    # audio in the container duration.
    # Matching either the source's video stream or its container is fine - a
    # stale per-stream tag on the source must not reject a good encode.
    out_video = video_stream_duration(out)
    src_video = video_stream_duration(source_info) or source_info.duration
    if (
        out_video > 0 and src_video > 0
        and abs(out_video - src_video) > max_duration_drift
        and abs(out_video - source_info.duration) > max_duration_drift
    ):
        return False, (
            f"Videospur ist {out_video:.1f}s lang statt {src_video:.1f}s - "
            "das Bild bricht vorzeitig ab"
        )
    if Path(output_path).stat().st_size < 1024:
        return False, "Ergebnisdatei ist leer"
    if source_info.audio_streams and not out.audio_streams:
        # Language and commentary rules can drop every track; a silent file
        # must never replace one with sound.
        return False, "Ergebnis hat keine Tonspur mehr"
    if plan is not None:
        want_audio = _kept(plan.audio)
        if len(out.audio_streams) != want_audio:
            return False, (
                f"Ergebnis hat {len(out.audio_streams)} Tonspur(en), geplant waren {want_audio}"
            )
        want_subs = _kept(plan.subtitles)
        if len(out.subtitle_streams) != want_subs:
            return False, (
                f"Ergebnis hat {len(out.subtitle_streams)} Untertitelspur(en), "
                f"geplant waren {want_subs}"
            )
    return True, ""
