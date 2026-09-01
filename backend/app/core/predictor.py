"""Size prediction: how big would this file be as AV1?

Two layers stacked on top of each other:

**Layer 1 - physics-ish heuristic.**  A target bitrate is derived from
resolution, framerate, CRF and an estimate of how complex the content is.  The
complexity estimate comes from the source's own bits-per-pixel, normalised for
how efficient the source codec is relative to AV1.  This runs on metadata alone,
so it is instant and works on a 40 TB library.

**Layer 2 - learned correction.**  Every finished encode writes back what
actually happened.  A ridge regression fits the *residual* between the heuristic
and reality in log space, so with zero samples the correction is exactly 1.0 and
the model degrades gracefully to layer 1.  After a few dozen encodes it has
learned the quirks of this library and this encoder build.

Sample encodes (see analyzer.py) short-circuit layer 1 with a real measurement;
layer 2 then corrects the sample-to-full-file extrapolation instead.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from . import codecs

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Layer 1: heuristic
# --------------------------------------------------------------------------- #

# Bits per pixel AV1 needs at CRF 30 / preset 6 for "average" live action, by
# resolution class.  Higher resolutions need fewer bits per pixel because detail
# is spread across more of them.
_BASE_BPP: list[tuple[int, float]] = [
    (480, 0.0750),
    (576, 0.0680),
    (720, 0.0550),
    (1080, 0.0400),
    (1440, 0.0320),
    (2160, 0.0220),
    (4320, 0.0150),
]

# How much bitrate AV1 needs relative to the source codec for the same quality.
# Lower = the source codec was less efficient = more headroom for us.
# Keys are canonical codec names (see core/codecs.py) - lookups normalise first,
# so "h265", "x265" and "hvc1" all land on "hevc".
CODEC_EFFICIENCY: dict[str, float] = {
    "av1": 1.00,
    "vvc": 1.15,
    "vp9": 0.78,
    "hevc": 0.75,
    "h264": 0.55,
    "vc1": 0.50,
    "vp8": 0.48, "wmv3": 0.48,
    "theora": 0.45,
    "mpeg4": 0.42, "wmv2": 0.42,
    "msmpeg4v3": 0.40,
    "mpeg2video": 0.35, "mpeg1video": 0.30,
    "prores": 0.06, "dnxhd": 0.06, "ffv1": 0.05, "huffyuv": 0.03, "rawvideo": 0.01,
}
DEFAULT_EFFICIENCY = 0.60

# +1 CRF removes roughly 9% of the bitrate for AV1 in this CRF range.
_CRF_SLOPE = 0.095
_REFERENCE_CRF = 30.0


def base_bpp(height: int) -> float:
    """Interpolate the reference bits-per-pixel for a given picture height."""
    h = max(1, height)
    if h <= _BASE_BPP[0][0]:
        return _BASE_BPP[0][1]
    if h >= _BASE_BPP[-1][0]:
        return _BASE_BPP[-1][1]
    for (h0, b0), (h1, b1) in zip(_BASE_BPP, _BASE_BPP[1:]):
        if h0 <= h <= h1:
            t = (h - h0) / (h1 - h0)
            # Interpolate in log space - bpp falls off geometrically.
            return math.exp(math.log(b0) * (1 - t) + math.log(b1) * t)
    return _BASE_BPP[-1][1]


def crf_factor(crf: float) -> float:
    """Bitrate multiplier relative to CRF 30."""
    return math.exp(-_CRF_SLOPE * (crf - _REFERENCE_CRF))


def codec_efficiency(codec: str) -> float:
    """How many bits this codec needs relative to AV1 for the same picture."""
    return CODEC_EFFICIENCY.get(codecs.normalise(codec), DEFAULT_EFFICIENCY)


@dataclass
class PredictionInput:
    """Everything the predictor needs about one file."""

    width: int
    height: int
    fps: float
    duration: float
    source_bitrate: int          # video stream bits/s
    source_codec: str
    bit_depth: int = 8
    is_hdr: bool = False
    is_animation: bool = False
    grain_level: float = 0.0     # 0..1, from sample analysis or the advisor
    crf: float = 30.0
    preset: int = 6
    target_height: int = 0       # 0 = keep source
    audio_bitrate: int = 0       # bits/s of the audio we intend to keep
    overhead_bitrate: int = 0    # subtitles, chapters, container

    @property
    def out_height(self) -> int:
        return self.target_height or self.height

    @property
    def out_width(self) -> int:
        if not self.target_height or not self.height:
            return self.width
        return int(round(self.width * self.target_height / self.height / 2) * 2)

    @property
    def out_pixels_per_second(self) -> float:
        return self.out_width * self.out_height * max(self.fps, 1.0)

    @property
    def source_bpp(self) -> float:
        pps = self.width * self.height * max(self.fps, 1.0)
        return self.source_bitrate / pps if pps > 0 and self.source_bitrate > 0 else 0.0


@dataclass
class Prediction:
    video_bitrate: float = 0.0       # predicted AV1 video bits/s
    total_bitrate: float = 0.0       # incl. audio + overhead
    size_bytes: int = 0
    saving_bytes: int = 0
    saving_pct: float = 0.0
    confidence: float = 0.4          # 0..1
    source: str = "heuristic"        # heuristic | sample | sample+model | model
    complexity: float = 1.0
    learned_correction: float = 1.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "video_bitrate": round(self.video_bitrate),
            "total_bitrate": round(self.total_bitrate),
            "size_bytes": self.size_bytes,
            "saving_bytes": self.saving_bytes,
            "saving_pct": round(self.saving_pct, 2),
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "complexity": round(self.complexity, 3),
            "learned_correction": round(self.learned_correction, 4),
            "notes": self.notes,
        }


def estimate_complexity(inp: PredictionInput) -> tuple[float, list[str]]:
    """How demanding is this content, relative to the reference clip?

    Derived from the source's bits-per-pixel translated into AV1 terms.  The
    exponent damps the signal hard on purpose: a 30 Mbit remux is mostly
    *wasteful*, not 6x more complex than a well-encoded web release.
    """
    notes: list[str] = []
    ref = base_bpp(inp.height)
    src_bpp = inp.source_bpp
    if src_bpp <= 0 or ref <= 0:
        return 1.0, ["Quell-Bitrate unbekannt - neutrale Komplexitaet angenommen"]

    equivalent = src_bpp * codec_efficiency(inp.source_codec)
    ratio = equivalent / ref
    complexity = ratio ** 0.35
    complexity = min(max(complexity, 0.55), 2.0)

    if ratio > 3.0:
        notes.append(f"Quelle liegt {ratio:.1f}x ueber der AV1-Referenzbitrate - viel Sparpotenzial")
    elif ratio < 0.9:
        notes.append(f"Quelle ist bereits sparsam ({ratio:.2f}x der AV1-Referenz)")

    if inp.is_animation:
        complexity *= 0.72
        notes.append("Animation erkannt - flaechige Bilder komprimieren deutlich besser")
    if inp.grain_level > 0.05:
        bump = 1.0 + min(inp.grain_level, 1.0) * 0.55
        complexity *= bump
        notes.append(f"Filmkorn erkannt (Level {inp.grain_level:.2f}) - Bitratenbedarf +{(bump-1)*100:.0f}%")
    if inp.is_hdr:
        complexity *= 1.08
        notes.append("HDR - etwas hoehere Bitrate noetig")
    if inp.bit_depth >= 10 and not inp.is_hdr:
        complexity *= 1.03

    return complexity, notes


def preset_factor(preset: int) -> float:
    """Faster SVT-AV1 presets produce slightly larger files at the same CRF."""
    # preset 4 is the reference; each step up costs roughly 2.5% efficiency.
    return 1.0 + max(0, preset - 4) * 0.025


def heuristic_bitrate(inp: PredictionInput) -> tuple[float, float, list[str]]:
    """Predicted AV1 video bitrate in bits/s, plus the complexity used."""
    complexity, notes = estimate_complexity(inp)
    bpp = base_bpp(inp.out_height) * crf_factor(inp.crf) * complexity * preset_factor(inp.preset)
    bitrate = bpp * inp.out_pixels_per_second
    return max(bitrate, 50_000.0), complexity, notes


# --------------------------------------------------------------------------- #
# Layer 2: learned residual correction
# --------------------------------------------------------------------------- #

FEATURE_KEYS = [
    "log_pixels",       # log of output pixels per second
    "crf",
    "preset",
    "log_source_bpp",
    "codec_eff",
    "is_hdr",
    "is_hw_encoder",
    "grain",
    "has_sample",
]


def build_features(inp: PredictionInput, encoder: str, has_sample: bool) -> dict[str, float]:
    return {
        "log_pixels": math.log(max(inp.out_pixels_per_second, 1.0)),
        "crf": float(inp.crf),
        "preset": float(inp.preset),
        "log_source_bpp": math.log(max(inp.source_bpp, 1e-4)),
        "codec_eff": codec_efficiency(inp.source_codec),
        "is_hdr": 1.0 if inp.is_hdr else 0.0,
        "is_hw_encoder": 1.0 if encoder in ("av1_qsv", "av1_vaapi") else 0.0,
        "grain": float(inp.grain_level),
        "has_sample": 1.0 if has_sample else 0.0,
    }


@dataclass
class LearnedModel:
    """Ridge regression on log(actual / predicted).

    Predicting the *residual* rather than the bitrate itself means an untrained
    model outputs 0 -> correction factor 1.0 -> pure heuristic.  No cold-start
    cliff, no way for a half-trained model to produce nonsense.
    """

    weights: np.ndarray | None = None
    mean: np.ndarray | None = None
    scale: np.ndarray | None = None
    intercept: float = 0.0
    n_samples: int = 0
    residual_std: float = 0.0
    mean_abs_error_pct: float = 0.0
    trained: bool = False
    trust_threshold: int = 15

    def fit(self, samples: Sequence[dict[str, Any]], trust_threshold: int = 15) -> None:
        """samples: [{features: {...}, predicted_bitrate, actual_bitrate}, ...]"""
        self.trust_threshold = max(3, trust_threshold)
        rows: list[list[float]] = []
        targets: list[float] = []
        for s in samples:
            feats = s.get("features") or {}
            pred = float(s.get("predicted_bitrate") or 0)
            actual = float(s.get("actual_bitrate") or 0)
            if pred <= 0 or actual <= 0:
                continue
            ratio = actual / pred
            # Guard against pathological outliers (failed encodes, corrupt files).
            if not (0.1 < ratio < 10.0):
                continue
            rows.append([float(feats.get(k, 0.0)) for k in FEATURE_KEYS])
            targets.append(math.log(ratio))

        self.n_samples = len(rows)
        if self.n_samples < 3:
            self.trained = False
            self.weights = None
            # Even below the fitting threshold, a plain mean offset is useful.
            if targets:
                self.intercept = float(np.mean(targets))
                self.residual_std = float(np.std(targets))
                self.trained = True
            return

        X = np.asarray(rows, dtype=float)
        y = np.asarray(targets, dtype=float)

        self.mean = X.mean(axis=0)
        scale = X.std(axis=0)
        scale[scale < 1e-9] = 1.0
        self.scale = scale
        Xs = (X - self.mean) / scale

        # Ridge penalty scaled to the sample count: heavy shrinkage when data is
        # thin, light once there is enough to support the coefficients.
        lam = max(1.0, 60.0 / max(self.n_samples, 1)) * self.n_samples * 0.05 + 1.0
        Xb = np.hstack([Xs, np.ones((len(Xs), 1))])
        penalty = np.eye(Xb.shape[1]) * lam
        penalty[-1, -1] = 0.0  # never penalise the intercept
        try:
            beta = np.linalg.solve(Xb.T @ Xb + penalty, Xb.T @ y)
        except np.linalg.LinAlgError:
            beta = np.linalg.lstsq(Xb, y, rcond=None)[0]

        self.weights = beta[:-1]
        self.intercept = float(beta[-1])
        residuals = y - (Xb @ beta)
        self.residual_std = float(np.std(residuals)) if len(residuals) > 1 else 0.0
        self.mean_abs_error_pct = float(np.mean(np.abs(np.expm1(residuals)))) * 100.0
        self.trained = True

    def correction(self, features: dict[str, float]) -> tuple[float, float]:
        """Return (multiplier, confidence_contribution)."""
        if not self.trained:
            return 1.0, 0.0
        if self.weights is None or self.mean is None or self.scale is None:
            adj = self.intercept
        else:
            x = np.asarray([float(features.get(k, 0.0)) for k in FEATURE_KEYS], dtype=float)
            xs = (x - self.mean) / self.scale
            adj = float(xs @ self.weights + self.intercept)

        # Blend towards "no correction" until there is enough evidence.
        weight = min(1.0, self.n_samples / float(self.trust_threshold))
        adj *= weight
        adj = max(-0.6, min(0.6, adj))  # never let the model swing wildly
        confidence = weight * (0.35 if self.residual_std < 0.25 else 0.2)
        return math.exp(adj), confidence

    def stats(self) -> dict[str, Any]:
        return {
            "trained": self.trained,
            "samples": self.n_samples,
            "trust_threshold": self.trust_threshold,
            "maturity": round(min(1.0, self.n_samples / float(self.trust_threshold)), 3),
            "residual_std": round(self.residual_std, 4),
            "mean_abs_error_pct": round(self.mean_abs_error_pct, 2),
            "top_signals": self._top_signals(),
        }

    def _top_signals(self) -> list[dict[str, Any]]:
        if self.weights is None:
            return []
        pairs = sorted(
            ({"feature": k, "weight": round(float(w), 4)} for k, w in zip(FEATURE_KEYS, self.weights)),
            key=lambda d: abs(d["weight"]),
            reverse=True,
        )
        return pairs[:4]


_model = LearnedModel()


def model() -> LearnedModel:
    return _model


def refit(samples: Sequence[dict[str, Any]], trust_threshold: int = 15) -> LearnedModel:
    _model.fit(samples, trust_threshold=trust_threshold)
    log.info(
        "Predictor refit on %d samples (trained=%s, mae=%.1f%%)",
        _model.n_samples, _model.trained, _model.mean_abs_error_pct,
    )
    return _model


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def predict(
    inp: PredictionInput,
    encoder: str = "libsvtav1",
    sample_bitrate: float | None = None,
    sample_spread: float | None = None,
    use_model: bool = True,
    source_size: int | None = None,
) -> Prediction:
    """Predict the AV1 size of one file.

    ``sample_bitrate`` is the measured video bitrate from trial encodes; when
    present it replaces the heuristic and only the learned correction is applied.
    ``sample_spread`` is the relative standard deviation across the samples and
    feeds the confidence score.
    """
    out = Prediction()
    heur_bitrate, complexity, notes = heuristic_bitrate(inp)
    out.complexity = complexity
    out.notes.extend(notes)

    if sample_bitrate and sample_bitrate > 0:
        # Trial encodes each begin with their own keyframe, which inflates them
        # slightly compared to a continuous encode of the whole file.
        base_bitrate = sample_bitrate * 0.955
        out.source = "sample"
        out.confidence = 0.75
        if sample_spread is not None:
            # Consistent samples -> trustworthy extrapolation.
            out.confidence = max(0.5, min(0.9, 0.9 - sample_spread))
            if sample_spread > 0.35:
                out.notes.append(
                    f"Szenen unterscheiden sich stark (Streuung {sample_spread*100:.0f}%) - "
                    "die Hochrechnung ist grober"
                )
    else:
        base_bitrate = heur_bitrate
        out.source = "heuristic"
        out.confidence = 0.45 if inp.source_bitrate > 0 else 0.25

    if use_model:
        features = build_features(inp, encoder, has_sample=bool(sample_bitrate))
        correction, conf_bonus = _model.correction(features)
        out.learned_correction = correction
        base_bitrate *= correction
        out.confidence = min(0.95, out.confidence + conf_bonus)
        if _model.trained and abs(correction - 1.0) > 0.02:
            direction = "hoeher" if correction > 1 else "niedriger"
            out.notes.append(
                f"Lernmodell korrigiert die Schaetzung um {abs(correction-1)*100:.0f}% {direction} "
                f"(aus {_model.n_samples} abgeschlossenen Jobs)"
            )
            out.source = f"{out.source}+model"

    out.video_bitrate = max(base_bitrate, 40_000.0)
    out.total_bitrate = out.video_bitrate + inp.audio_bitrate + inp.overhead_bitrate
    duration = max(inp.duration, 1.0)
    out.size_bytes = int(out.total_bitrate * duration / 8)

    if source_size and source_size > 0:
        out.saving_bytes = source_size - out.size_bytes
        out.saving_pct = out.saving_bytes / source_size * 100.0
    return out
