"""The decision engine: should this file become AV1, and with which settings?

Three depths, picked in the settings:

``quick``   metadata only.  Milliseconds per file, good for a first pass over a
            whole library.
``sample``  cuts a few short segments out of the file, encodes them for real and
            measures the resulting bitrate.  This is what turns a guess into a
            number, and it is the default.
``vmaf``    additionally runs a CRF search against a VMAF target, so each file
            gets the highest CRF that still hits the quality bar.

Every path ends in the same place: a prediction, a plan, and a decision with a
reason a human can read.  The guiding rule is that a file must never come out
bigger than it went in, so uncertainty always resolves towards skipping.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AppSettings
from . import codecs, ffmpeg, planner, predictor, quality
from .advisor import Advisor, Advice
from .hwaccel import HardwareReport
from .planner import EncodePlan

log = logging.getLogger(__name__)


@dataclass
class SampleResult:
    """Outcome of the trial encodes."""

    measured_bitrate: float = 0.0     # bits/s, mean across segments
    spread: float = 0.0               # relative std dev across segments
    segments: int = 0
    grain_level: float = 0.0
    vmaf: float | None = None          # on the VMAF scale, whichever metric was used
    quality_metric: str = ""           # "vmaf" | "ssim" | "" when not measured
    crf_used: float = 0.0
    encode_seconds: float = 0.0
    speed_factor: float = 0.0         # source seconds encoded per wall second
    ok: bool = False
    error: str = ""


@dataclass
class AnalysisResult:
    decision: str = "skip"            # convert | skip | error
    reason: str = ""
    reasons: list[str] = field(default_factory=list)
    plan: EncodePlan | None = None
    prediction: predictor.Prediction | None = None
    sample: SampleResult | None = None
    advice: Advice | None = None
    depth: str = "quick"
    estimated_size: int = 0
    estimated_saving_bytes: int = 0
    estimated_saving_pct: float = 0.0
    confidence: float = 0.0
    eta_seconds: int = 0

    @property
    def should_convert(self) -> bool:
        return self.decision == "convert"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "reasons": self.reasons,
            "depth": self.depth,
            "estimated_size": self.estimated_size,
            "estimated_saving_bytes": self.estimated_saving_bytes,
            "estimated_saving_pct": round(self.estimated_saving_pct, 2),
            "confidence": round(self.confidence, 3),
            "eta_seconds": self.eta_seconds,
            "plan": self.plan.to_dict() if self.plan else None,
            "prediction": self.prediction.as_dict() if self.prediction else None,
            "sample": self.sample.__dict__ if self.sample else None,
            "advice": self.advice.to_dict() if self.advice else None,
        }


# --------------------------------------------------------------------------- #
# Cheap pre-checks
# --------------------------------------------------------------------------- #

def _min_useful_bitrate(height: int, crf: float) -> float:
    """Below this the source is already leaner than our AV1 target would be."""
    return predictor.base_bpp(height) * predictor.crf_factor(crf) * 0.85


def precheck(info: ffmpeg.MediaInfo, settings: AppSettings) -> tuple[bool, str]:
    """Fast rejections that need no encoding at all.  Returns (skip, reason)."""
    codec = codecs.normalise(info.video_codec)
    if codecs.is_excluded(codec, settings.analysis.skip_codecs):
        # AV1 is excluded because re-encoding it is pointless; everything else
        # on that list is a deliberate choice, and saying "would not save
        # anything" about HEVC would simply be wrong.
        if codec == "av1":
            return True, "Bereits AV1 - eine Neukodierung wuerde nur Qualitaet kosten."
        return True, f"{codecs.label(codec)} {codecs.EXCLUSION_REASON}"
    if info.duration and info.duration < settings.library.min_duration_seconds:
        return True, (
            f"Nur {info.duration:.0f}s lang - unter der Mindestlaenge von "
            f"{settings.library.min_duration_seconds}s."
        )
    if info.size and info.size < settings.library.min_file_size_mb * 1024 * 1024:
        return True, f"Datei ist nur {info.size / 1024 / 1024:.0f} MB - zu klein zum Optimieren."
    if not info.width or not info.height:
        return True, "Keine gueltigen Bildmasse gefunden."

    if info.video_bitrate > 0:
        floor_kbps = settings.analysis.skip_if_bitrate_below_kbps
        if floor_kbps:
            if info.video_bitrate < floor_kbps * 1000:
                return True, (
                    f"Video-Bitrate {info.video_bitrate // 1000} kbit/s liegt unter der "
                    f"eingestellten Untergrenze von {floor_kbps} kbit/s."
                )
        else:
            pps = info.width * info.height * max(info.fps, 1.0)
            min_bpp = _min_useful_bitrate(info.height, settings.encoding.crf)
            equivalent = info.bits_per_pixel * predictor.codec_efficiency(codec)
            if equivalent < min_bpp:
                return True, (
                    f"Quelle ist bereits sehr sparsam kodiert "
                    f"({info.video_bitrate // 1000} kbit/s bei {info.height}p) - "
                    "AV1 wuerde hier eher groesser werden."
                )
    return False, ""


# --------------------------------------------------------------------------- #
# Trial encodes
# --------------------------------------------------------------------------- #

async def _encode_segment(
    plan: EncodePlan, info: ffmpeg.MediaInfo, segment: str, dest: str, timeout: float
) -> tuple[int, float]:
    """Encode one extracted segment, return (bytes, duration)."""
    args = planner.build_ffmpeg_args(plan, info, segment, dest, quiet_streams=True)
    code, err = await ffmpeg.run_with_progress(args, timeout=timeout)
    if code != 0:
        raise ffmpeg.FFmpegError(f"Testencode fehlgeschlagen: {err.strip()[-300:]}", code, err)
    size = os.path.getsize(dest) if os.path.exists(dest) else 0
    try:
        out = await ffmpeg.probe(dest, timeout=30)
        duration = out.duration
    except ffmpeg.FFmpegError:
        duration = 0.0
    return size, duration


async def run_samples(
    info: ffmpeg.MediaInfo,
    plan: EncodePlan,
    settings: AppSettings,
    workdir: Path,
    measure_grain: bool = True,
    cancel_event: asyncio.Event | None = None,
) -> SampleResult:
    """Cut segments, encode them, and measure what AV1 really costs here."""
    result = SampleResult(crf_used=plan.crf)
    cfg = settings.analysis
    count = max(1, cfg.sample_count)
    duration = min(cfg.sample_duration, max(4.0, info.duration / (count * 2) if info.duration else 12))
    positions = planner.sample_positions(info.duration, count, cfg.sample_skip_intro_pct)

    sizes: list[float] = []
    total_source_seconds = 0.0
    loop = asyncio.get_running_loop()
    started = loop.time()

    for i, start in enumerate(positions):
        if cancel_event is not None and cancel_event.is_set():
            result.error = "abgebrochen"
            return result
        raw = workdir / f"seg{i}.mkv"
        enc = workdir / f"seg{i}.av1.mkv"
        try:
            await ffmpeg.extract_segment(info.path, start, duration, str(raw), timeout=300)
        except ffmpeg.FFmpegError as exc:
            log.debug("segment %d extraction failed: %s", i, exc)
            continue

        if measure_grain and i == 0:
            try:
                result.grain_level = await quality.measure_grain(str(raw))
            except Exception as exc:  # never let the probe kill the analysis
                log.debug("grain probe failed: %s", exc)

        try:
            size, seg_duration = await _encode_segment(
                plan, info, str(raw), str(enc), timeout=900
            )
        except ffmpeg.FFmpegError as exc:
            result.error = str(exc)
            log.warning("trial encode failed for %s: %s", info.path, exc)
            continue

        effective = seg_duration or duration
        if size > 0 and effective > 0:
            sizes.append(size * 8 / effective)
            total_source_seconds += effective
        for f in (raw, enc):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    result.encode_seconds = loop.time() - started
    result.segments = len(sizes)
    if not sizes:
        result.ok = False
        if not result.error:
            result.error = "Keine Testsegmente konnten kodiert werden"
        return result

    mean = sum(sizes) / len(sizes)
    result.measured_bitrate = mean
    if len(sizes) > 1 and mean > 0:
        variance = sum((s - mean) ** 2 for s in sizes) / len(sizes)
        result.spread = (variance ** 0.5) / mean
    if result.encode_seconds > 0 and total_source_seconds > 0:
        result.speed_factor = total_source_seconds / result.encode_seconds
    result.ok = True
    return result


async def search_crf_for_quality(
    info: ffmpeg.MediaInfo,
    plan: EncodePlan,
    settings: AppSettings,
    workdir: Path,
    cancel_event: asyncio.Event | None = None,
) -> tuple[float, quality.QualityScore | None, list[str]]:
    """Find the highest CRF that still reaches the quality target.

    Uses one representative segment and a secant-style search: quality moves
    close to linearly with CRF over the range we care about, so each measurement
    lets us jump straight to a much better guess instead of halving an interval.

    Scores are compared on the VMAF scale regardless of which metric the ffmpeg
    build actually provides - see ``quality.py``.
    """
    notes: list[str] = []
    target = settings.analysis.target_vmaf
    steps = max(1, settings.analysis.vmaf_search_steps)

    metric = await quality.available_metric()
    if metric == "none":
        notes.append(
            "Dieser ffmpeg-Build kennt weder VMAF noch SSIM - die Qualitaetssuche entfaellt."
        )
        return plan.crf, None, notes

    seg = workdir / "quality_ref.mkv"
    position = planner.sample_positions(info.duration, 1, 0.15)[0]
    seg_len = min(float(settings.analysis.sample_duration), 15.0)
    try:
        await ffmpeg.extract_segment(info.path, position, seg_len, str(seg), timeout=300)
    except ffmpeg.FFmpegError as exc:
        notes.append(f"Qualitaetssuche uebersprungen: Referenzsegment nicht lesbar ({exc})")
        return plan.crf, None, notes

    crf = plan.crf
    best_crf: float = crf
    best_score: quality.QualityScore | None = None
    history: list[tuple[float, float]] = []

    for step in range(steps):
        if cancel_event is not None and cancel_event.is_set():
            break
        trial_plan = EncodePlan(**{**plan.to_dict(), "crf": crf})
        out = workdir / f"quality_try{step}.mkv"
        try:
            await _encode_segment(trial_plan, info, str(seg), str(out), timeout=900)
        except ffmpeg.FFmpegError as exc:
            notes.append(f"Qualitaetssuche abgebrochen: {exc}")
            break

        score = await quality.measure_quality(str(seg), str(out), threads=4)
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        if score is None:
            notes.append("Qualitaet konnte nicht gemessen werden - Standard-CRF wird verwendet.")
            break

        scaled = score.vmaf_estimate
        history.append((crf, scaled))
        if (
            best_score is None
            or (scaled >= target and crf > best_crf)
            or (best_score.vmaf_estimate < target and scaled > best_score.vmaf_estimate)
        ):
            best_crf, best_score = crf, score

        gap = scaled - target
        if abs(gap) <= 0.4:
            notes.append(f"CRF {crf:g} trifft das Qualitaetsziel ({score.describe()}).")
            break
        if step == steps - 1:
            break

        # Slope from the two most recent points, or a sane default.
        slope = 0.85  # points lost per CRF step, on the VMAF scale
        if len(history) >= 2:
            (c0, v0), (c1, v1) = history[-2], history[-1]
            if abs(c1 - c0) > 0.01:
                measured = abs((v1 - v0) / (c1 - c0))
                if 0.15 < measured < 4.0:
                    slope = measured
        move = max(-6.0, min(6.0, gap / slope))
        next_crf = planner.clamp_crf(round(crf + move), settings)
        if abs(next_crf - crf) < 1:
            break
        crf = next_crf

    try:
        seg.unlink(missing_ok=True)
    except OSError:
        pass

    if best_score is not None:
        if best_score.vmaf_estimate < target - 1.0:
            notes.append(
                f"Qualitaetsziel {target:.0f} wurde selbst bei CRF {best_crf:g} nicht ganz "
                f"erreicht (gemessen {best_score.describe()})."
            )
        elif best_crf != plan.crf:
            notes.append(
                f"CRF von {plan.crf:g} auf {best_crf:g} angepasst - {best_score.describe()} "
                f"bei Ziel {target:.0f}."
            )
    return best_crf, best_score, notes


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

async def analyze(
    info: ffmpeg.MediaInfo,
    settings: AppSettings,
    hw: HardwareReport | None,
    advisor: Advisor | None = None,
    depth: str | None = None,
    workroot: Path | None = None,
    cancel_event: asyncio.Event | None = None,
) -> AnalysisResult:
    """Full analysis for one file."""
    result = AnalysisResult()
    mode = depth or settings.analysis.mode
    result.depth = mode

    # --- 1. cheap rejections -------------------------------------------------
    skip, reason = precheck(info, settings)
    if skip:
        result.decision = "skip"
        result.reason = reason
        result.reasons = [reason]
        result.confidence = 0.9
        return result

    # --- 2. initial plan -----------------------------------------------------
    plan = planner.build_plan(info, settings, hw)
    result.reasons.extend(plan.notes)

    is_animation = quality.looks_like_animation(info.path)
    audio_bitrate = planner.estimate_audio_bitrate(plan, info)
    overhead = planner.estimate_overhead_bitrate(info)

    def make_input(crf: float, grain: float) -> predictor.PredictionInput:
        return predictor.PredictionInput(
            width=info.width, height=info.height, fps=info.fps, duration=info.duration,
            source_bitrate=info.video_bitrate, source_codec=info.video_codec,
            bit_depth=info.bit_depth, is_hdr=info.is_hdr, is_animation=is_animation,
            grain_level=grain, crf=crf, preset=plan.preset,
            target_height=plan.target_height, audio_bitrate=audio_bitrate,
            overhead_bitrate=overhead,
        )

    grain_level = 0.0
    sample: SampleResult | None = None
    workdir: Path | None = None

    # --- 3. quick estimate ---------------------------------------------------
    quick = predictor.predict(
        make_input(plan.crf, grain_level),
        encoder=plan.encoder,
        use_model=settings.analysis.use_learning_model,
        source_size=info.size,
    )

    # Skip the expensive path when the cheap one already says "hopeless".
    hopeless = quick.saving_pct < (settings.analysis.min_saving_percent - 25.0)
    if mode != "quick" and hopeless:
        result.reasons.append(
            "Schnellschaetzung liegt weit unter der Zielersparnis - Testkodierung "
            "wurde eingespart."
        )
        mode = "quick"
        result.depth = "quick"

    try:
        # --- 4. real measurements -------------------------------------------
        if mode in ("sample", "vmaf"):
            root = workroot or Path(tempfile.gettempdir())
            root.mkdir(parents=True, exist_ok=True)
            workdir = Path(tempfile.mkdtemp(prefix="optimizarr-analyze-", dir=str(root)))

            measured_quality: quality.QualityScore | None = None
            if mode == "vmaf" and settings.encoding.allow_crf_adjust:
                new_crf, measured_quality, notes = await search_crf_for_quality(
                    info, plan, settings, workdir, cancel_event
                )
                result.reasons.extend(notes)
                plan.crf = planner.clamp_crf(new_crf, settings)
                if measured_quality is not None:
                    result.reasons.append(f"Gemessene Qualitaet: {measured_quality.describe()}")

            sample = await run_samples(info, plan, settings, workdir, cancel_event=cancel_event)
            if sample.ok and measured_quality is not None:
                sample.vmaf = measured_quality.vmaf_estimate
                sample.quality_metric = measured_quality.metric
            if sample.ok:
                grain_level = sample.grain_level
                result.reasons.append(
                    f"{sample.segments} Testsegmente kodiert - gemessene AV1-Bitrate "
                    f"{sample.measured_bitrate / 1000:.0f} kbit/s"
                )
                if sample.speed_factor:
                    result.reasons.append(
                        f"Encoder-Tempo ca. {sample.speed_factor:.1f}x Echtzeit"
                    )
                # Grain synthesis is decided from the measurement, not a guess.
                if settings.encoding.auto_film_grain and not plan.is_hardware:
                    level = quality.grain_synthesis_level(grain_level, info.is_hdr)
                    if level and not settings.encoding.film_grain_synthesis:
                        plan.film_grain = level
                        result.reasons.append(
                            f"Filmkorn-Synthese Stufe {level} aktiviert - das spart bei "
                            "koernigem Material deutlich Bitrate."
                        )
            else:
                result.reasons.append(
                    f"Testkodierung nicht moeglich ({sample.error}) - es gilt die Schaetzung "
                    "aus den Metadaten."
                )
                result.depth = "quick"

        # --- 5. optional Claude review --------------------------------------
        advice: Advice | None = None
        interim = predictor.predict(
            make_input(plan.crf, grain_level),
            encoder=plan.encoder,
            sample_bitrate=sample.measured_bitrate if sample and sample.ok else None,
            sample_spread=sample.spread if sample and sample.ok else None,
            use_model=settings.analysis.use_learning_model,
            source_size=info.size,
        )

        if advisor is not None and advisor.should_ask(interim.confidence):
            context = _advisor_context(info, plan, interim, sample, grain_level, settings)
            advice = await advisor.advise(context, filename=Path(info.path).name)
            if advice.ok:
                if advice.crf_delta:
                    old = plan.crf
                    plan.crf = planner.clamp_crf(plan.crf + advice.crf_delta, settings)
                    if plan.crf != old:
                        result.reasons.append(
                            f"KI-Berater passt CRF von {old:g} auf {plan.crf:g} an "
                            f"(erkannt: {advice.content_type})"
                        )
                if advice.film_grain_override >= 0 and not plan.is_hardware:
                    plan.film_grain = advice.film_grain_override
                    result.reasons.append(
                        f"KI-Berater setzt Filmkorn-Synthese auf {advice.film_grain_override}"
                    )
                if advice.reasoning:
                    result.reasons.append(f"KI-Berater: {advice.reasoning}")
                for warn in advice.warnings:
                    result.reasons.append(f"Hinweis: {warn}")
            elif advice.error:
                result.reasons.append(f"KI-Berater nicht erreichbar: {advice.error}")
        result.advice = advice

    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)

    # --- 6. final prediction with the settings we actually landed on ---------
    final = predictor.predict(
        make_input(plan.crf, grain_level),
        encoder=plan.encoder,
        sample_bitrate=_rescale_sample(sample, plan, settings) if sample and sample.ok else None,
        sample_spread=sample.spread if sample and sample.ok else None,
        use_model=settings.analysis.use_learning_model,
        source_size=info.size,
    )
    result.reasons.extend(n for n in final.notes if n not in result.reasons)

    plan.estimated_size = final.size_bytes
    plan.estimated_saving_bytes = final.saving_bytes
    plan.estimated_saving_pct = final.saving_pct
    plan.predicted_video_bitrate = int(final.video_bitrate)

    result.plan = plan
    result.prediction = final
    result.sample = sample
    result.estimated_size = final.size_bytes
    result.estimated_saving_bytes = final.saving_bytes
    result.estimated_saving_pct = final.saving_pct
    result.confidence = final.confidence
    result.eta_seconds = _estimate_eta(info, sample, plan)

    # --- 7. verdict ----------------------------------------------------------
    _decide(result, info, settings, advice=result.advice)
    return result


def _rescale_sample(sample: SampleResult, plan: EncodePlan, settings: AppSettings) -> float:
    """Adjust a measured sample bitrate if CRF moved after the measurement."""
    if not sample.crf_used or sample.crf_used == plan.crf:
        return sample.measured_bitrate
    ratio = predictor.crf_factor(plan.crf) / predictor.crf_factor(sample.crf_used)
    return sample.measured_bitrate * ratio


def _estimate_eta(info: ffmpeg.MediaInfo, sample: SampleResult | None, plan: EncodePlan) -> int:
    """Rough encode duration in seconds."""
    if sample and sample.ok and sample.speed_factor > 0:
        return int(info.duration / sample.speed_factor)
    # Fall back to coarse throughput guesses when no measurement exists.
    pixels = max(1, info.width * info.height)
    if plan.is_hardware:
        base = 4.0 if pixels <= 1920 * 1080 else 1.5     # x realtime
    else:
        base = 1.2 if pixels <= 1920 * 1080 else 0.35
        base *= 1.0 + max(0, plan.preset - 6) * 0.35
    return int(info.duration / max(base, 0.05))


def _decide(
    result: AnalysisResult,
    info: ffmpeg.MediaInfo,
    settings: AppSettings,
    advice: Advice | None = None,
) -> None:
    """Apply the thresholds and write a human-readable verdict."""
    cfg = settings.analysis
    saving_pct = result.estimated_saving_pct
    saving_bytes = result.estimated_saving_bytes
    min_pct = cfg.min_saving_percent
    min_bytes = cfg.min_saving_mb * 1024 * 1024

    # A shaky estimate has to clear a higher bar - this is the rule that keeps
    # files from coming out bigger than they went in.
    if result.confidence < 0.5:
        min_pct += 10.0
        result.reasons.append(
            "Schaetzung ist unsicher - die Mindestersparnis wurde um 10 Prozentpunkte angehoben."
        )
    elif result.confidence < 0.65:
        min_pct += 5.0

    if saving_bytes <= 0:
        result.decision = "skip"
        result.reason = (
            f"AV1 waere hier voraussichtlich groesser ({_fmt(result.estimated_size)} statt "
            f"{_fmt(info.size)}) - die Datei bleibt unveraendert."
        )
    elif saving_pct < min_pct:
        result.decision = "skip"
        result.reason = (
            f"Nur {saving_pct:.0f}% Ersparnis erwartet ({_fmt(saving_bytes)}) - "
            f"unter der Schwelle von {min_pct:.0f}%."
        )
    elif saving_bytes < min_bytes:
        result.decision = "skip"
        result.reason = (
            f"Ersparnis von {_fmt(saving_bytes)} liegt unter der Mindestgroesse von "
            f"{cfg.min_saving_mb} MB."
        )
    elif advice is not None and advice.ok and not advice.recommend_convert:
        result.decision = "skip"
        result.reason = (
            "Der KI-Berater raet ab: "
            + (advice.reasoning or "Konvertierung lohnt sich bei diesem Material nicht.")
        )
    else:
        result.decision = "convert"
        result.reason = (
            f"Spart voraussichtlich {_fmt(saving_bytes)} ({saving_pct:.0f}%): "
            f"{_fmt(info.size)} -> {_fmt(result.estimated_size)}."
        )
    if result.reason not in result.reasons:
        result.reasons.insert(0, result.reason)


def _advisor_context(
    info: ffmpeg.MediaInfo,
    plan: EncodePlan,
    prediction: predictor.Prediction,
    sample: SampleResult | None,
    grain: float,
    settings: AppSettings,
) -> dict[str, Any]:
    """The structured facts handed to Claude."""
    ctx: dict[str, Any] = {
        "source": {
            "codec": info.video_codec,
            "profile": info.profile,
            "resolution": f"{info.width}x{info.height}",
            "fps": round(info.fps, 3),
            "duration_minutes": round(info.duration / 60, 1),
            "video_bitrate_kbps": info.video_bitrate // 1000,
            "bits_per_pixel": round(info.bits_per_pixel, 5),
            "bit_depth": info.bit_depth,
            "hdr": info.hdr_format or ("hdr" if info.is_hdr else "sdr"),
            "interlaced": info.interlaced,
            "size_gb": round(info.size / 1024**3, 2),
            "audio_tracks": [
                {"codec": a["codec"], "channels": a["channels"], "language": a["language"]}
                for a in info.audio_streams[:6]
            ],
        },
        "measurements": {
            "grain_level_0_to_1": round(grain, 3),
            "grain_probe_available": sample is not None and sample.ok,
        },
        "proposed_plan": {
            "encoder": plan.encoder,
            "crf": plan.crf,
            "preset": plan.preset,
            "film_grain": plan.film_grain,
            "pix_fmt": plan.pix_fmt,
            "crf_allowed_range": [settings.encoding.crf_min, settings.encoding.crf_max],
        },
        "local_prediction": {
            "predicted_av1_bitrate_kbps": int(prediction.video_bitrate // 1000),
            "predicted_size_gb": round(prediction.size_bytes / 1024**3, 2),
            "predicted_saving_percent": round(prediction.saving_pct, 1),
            "confidence_0_to_1": round(prediction.confidence, 2),
            "basis": prediction.source,
        },
        "quality_target_vmaf": settings.analysis.target_vmaf,
    }
    if sample and sample.ok:
        ctx["measurements"]["trial_encode_bitrate_kbps"] = int(sample.measured_bitrate // 1000)
        ctx["measurements"]["trial_segments"] = sample.segments
        ctx["measurements"]["scene_variation"] = round(sample.spread, 3)
        if sample.vmaf is not None:
            ctx["measurements"]["measured_vmaf"] = round(sample.vmaf, 1)
    return ctx


def _fmt(num: int | float) -> str:
    """Human file size."""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024.0
    return f"{value:.1f} PiB"
