"""Sample-based quality checks of completed encodes."""
from __future__ import annotations
import asyncio
import logging
import math
import shutil
from pathlib import Path
from ..config import TRANSCODE_DIR
from . import ffmpeg, planner, quality
from .encode_types import JobCancelled
log = logging.getLogger(__name__)

def _mid_frame(start: float, fps: float) -> float:
    """Move a cut position to halfway between two frames.

    The quality check pairs the two slices frame by frame, so both must begin
    on the same picture.  Source and encode may disagree about a timestamp by
    a millisecond (Matroska rounding); a cut exactly on a frame could then
    start one slice a frame later than the other.  Half a frame away from
    every timestamp, both cuts pick the same first frame.
    """
    if fps <= 0 or start <= 0:
        return start
    return (math.floor(start * fps) + 0.5) / fps


async def _spot_check_quality(
    source: str, output: str, info: ffmpeg.MediaInfo,
    cancel: asyncio.Event | None = None,
    minimum_samples: int = 2,
) -> quality.QualityScore | None:
    """Measure a few short slices - scoring a whole film would take hours.

    ``cancel`` stops the cuts and comparisons that are running; the job then
    ends as cancelled (:class:`JobCancelled`), not as a failed measurement.
    """
    import tempfile

    workdir = Path(tempfile.mkdtemp(prefix="optimizarr-quality-", dir=str(TRANSCODE_DIR)))
    scores: list[quality.QualityScore] = []
    try:
        positions = planner.sample_positions(info.duration, 2, 0.1)
        for i, start in enumerate(positions):
            if cancel is not None and cancel.is_set():
                raise JobCancelled()
            start = _mid_frame(start, info.fps)
            ref = workdir / f"ref{i}.mkv"
            dist = workdir / f"dist{i}.mkv"
            try:
                await ffmpeg.extract_segment(
                    source, start, 10.0, str(ref), timeout=180, exact=True, cancel_event=cancel,
                )
                await ffmpeg.extract_segment(
                    output, start, 10.0, str(dist), timeout=180, exact=True, cancel_event=cancel,
                )
            except ffmpeg.FFmpegCancelled:
                raise JobCancelled() from None
            except ffmpeg.FFmpegError as exc:
                log.warning("quality slice at %.0fs failed: %s", start, exc)
                continue
            # The source size from the probe: a downscaled encode is judged on
            # the canvas the viewer actually sees.
            try:
                score = await quality.measure_quality(
                    str(ref), str(dist), threads=4, timeout=900,
                    width=info.width, height=info.height, cancel_event=cancel,
                )
            except ffmpeg.FFmpegCancelled:
                raise JobCancelled() from None
            if score is not None:
                scores.append(score)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if len(scores) < minimum_samples or len({s.metric for s in scores}) != 1:
        return None
    return quality.QualityScore(
        value=sum(s.value for s in scores) / len(scores),
        metric=scores[0].metric,
        vmaf_estimate=sum(s.vmaf_estimate for s in scores) / len(scores),
        successful=len(scores), planned=len(positions), worst_vmaf=min(s.vmaf_estimate for s in scores),
    )
