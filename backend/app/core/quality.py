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

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg

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


async def _measure_vmaf(
    reference: str, distorted: str, threads: int, scale_to_reference: bool, timeout: float
) -> float | None:
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    log_path = tmp.name
    # ffmpeg's filtergraph parser treats ':' and '\' as separators.
    escaped = log_path.replace("\\", "/").replace(":", r"\:")

    dist_chain = "[0:v]setpts=PTS-STARTPTS"
    if scale_to_reference:
        # Judge a downscaled encode on the canvas the viewer actually sees.
        dist_chain += ",scale=rw:rh:flags=bicubic"
    graph = (
        f"{dist_chain}[dist];[1:v]setpts=PTS-STARTPTS[ref];"
        f"[dist][ref]libvmaf=log_fmt=json:log_path={escaped}:n_threads={max(1, threads)}"
    )

    try:
        code, _, err = await ffmpeg.run_simple(
            ["-i", distorted, "-i", reference, "-lavfi", graph, "-f", "null", "-"], timeout=timeout
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
    except (OSError, json.JSONDecodeError, ffmpeg.FFmpegError) as exc:
        log.warning("VMAF measurement failed: %s", exc)
        return None
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass


async def _measure_ssim(
    reference: str, distorted: str, scale_to_reference: bool, timeout: float
) -> float | None:
    dist_chain = "[0:v]setpts=PTS-STARTPTS"
    if scale_to_reference:
        dist_chain += ",scale=rw:rh:flags=bicubic"
    graph = f"{dist_chain}[dist];[1:v]setpts=PTS-STARTPTS[ref];[dist][ref]ssim"

    try:
        code, _, err = await ffmpeg.run_simple(
            ["-i", distorted, "-i", reference, "-lavfi", graph, "-f", "null", "-"], timeout=timeout
        )
    except ffmpeg.FFmpegError as exc:
        log.warning("SSIM measurement failed: %s", exc)
        return None
    if code != 0:
        log.warning("SSIM run failed: %s", err.strip()[-300:])
        return None
    match = _SSIM_RE.search(err)
    if not match:
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
) -> QualityScore | None:
    """Compare ``distorted`` against ``reference``.

    Returns ``None`` only when no comparison filter exists at all, so callers
    can carry on without a quality gate instead of failing the job.
    """
    metric = await available_metric()
    if metric == "vmaf":
        score = await _measure_vmaf(reference, distorted, threads, scale_to_reference, timeout)
        if score is not None:
            return QualityScore(value=score, metric="vmaf", vmaf_estimate=score)
        # A failed VMAF run is worth retrying as SSIM rather than giving up.
        metric = "ssim"
    if metric == "ssim":
        ssim = await _measure_ssim(reference, distorted, scale_to_reference, timeout)
        if ssim is not None:
            return QualityScore(value=ssim, metric="ssim", vmaf_estimate=ssim_to_vmaf(ssim))
        return None
    log.warning("no quality metric available in this ffmpeg build")
    return None


async def measure_grain(sample_path: str, timeout: float = 300.0) -> float:
    """Estimate how much film grain / sensor noise a clip carries (0..1).

    Denoise the clip and compare it against itself: the more the denoiser
    changes, the more high-frequency noise there was.  Grainy sources are the
    classic case where naive AV1 settings *grow* a file, and the classic case
    where grain synthesis wins big - so it is worth measuring rather than
    guessing.
    """
    graph = "split[a][b];[a]hqdn3d=4:4:9:9[den];[b][den]psnr"
    try:
        code, _, err = await ffmpeg.run_simple(
            ["-i", sample_path, "-lavfi", graph, "-f", "null", "-"], timeout=timeout
        )
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


async def verify_output(
    source_info: ffmpeg.MediaInfo,
    output_path: str,
    max_duration_drift: float = 2.0,
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
    if Path(output_path).stat().st_size < 1024:
        return False, "Ergebnisdatei ist leer"
    return True, ""
