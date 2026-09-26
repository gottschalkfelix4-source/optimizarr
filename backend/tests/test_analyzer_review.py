"""Analyzer: Dolby Vision verdicts, learning-model features, cancelled scans."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(Path(tempfile.gettempdir()) / "optimizarr-pytest/config"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AppSettings  # noqa: E402
from app.core import analyzer, predictor  # noqa: E402
from app.core.advisor import Advice  # noqa: E402
from app.core.ffmpeg import MediaInfo  # noqa: E402


def settings(dv="skip", **analysis) -> AppSettings:
    cfg = AppSettings()
    cfg.analysis.dolby_vision = dv
    for key, value in analysis.items():
        setattr(cfg.analysis, key, value)
    return cfg


def uhd(**kw) -> MediaInfo:
    values = dict(path="/media/Film (2020)/Film.mkv", size=40 * 1024**3, duration=7200,
                  video_codec="hevc", width=3840, height=2160, fps=23.976,
                  video_bitrate=45_000_000, bit_depth=10, is_hdr=True,
                  color_transfer="smpte2084", hdr_format="hdr10")
    values.update(kw)
    return MediaInfo(**values)


# --------------------------------------------------------------------------- #
# Dolby Vision
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("dv", ["skip", "hdr10_fallback"])
def test_profile5_is_always_skipped(dv):
    skip, reason = analyzer.precheck(uhd(hdr_format="dolby_vision_p5", color_transfer=""), settings(dv))
    assert skip and "Profil 5" in reason and "nie" in reason


def test_profile5_is_refused_even_when_forced():
    assert analyzer.dolby_vision_block("dolby_vision_p5", "smpte2084", settings(), force=True)


@pytest.mark.parametrize("fmt", ["dolby_vision_p8", "dolby_vision_p7"])
def test_profiles_with_a_base_layer_follow_the_setting(fmt):
    skip, reason = analyzer.precheck(uhd(hdr_format=fmt), settings("skip"))
    assert skip and "Dolby Vision" in reason and "HDR10" in reason
    assert analyzer.precheck(uhd(hdr_format=fmt), settings("hdr10_fallback")) == (False, "")
    # Forcing may convert P7/P8 even when the setting says skip.
    assert analyzer.dolby_vision_block(fmt, "smpte2084", settings("skip"), force=True) == ""


def test_unknown_profile_needs_a_pq_base():
    no_pq = uhd(hdr_format="dolby_vision", color_transfer="bt709")
    assert analyzer.precheck(no_pq, settings("hdr10_fallback"))[0]
    assert analyzer.dolby_vision_block("dolby_vision", "bt709", settings(), force=True)
    pq = uhd(hdr_format="dolby_vision", color_transfer="smpte2084")
    assert analyzer.precheck(pq, settings("hdr10_fallback")) == (False, "")


def test_dolby_vision_beats_the_h264_migration():
    info = uhd(video_codec="h264", hdr_format="dolby_vision_p5", color_transfer="")
    assert analyzer.precheck(info, settings(convert_all_h264=True))[0]


def test_hdr10_fallback_says_what_is_lost():
    result = asyncio.run(analyzer.analyze(
        uhd(hdr_format="dolby_vision_p8"), settings("hdr10_fallback"), None, depth="quick",
    ))
    assert any("als HDR10 kodiert" in r for r in result.reasons)


def test_plain_hdr10_is_unaffected():
    assert analyzer.precheck(uhd(), settings("skip")) == (False, "")


def test_reason_means_convert():
    result = asyncio.run(analyzer.analyze(uhd(), settings(), None, depth="quick"))
    assert analyzer.reason_means_convert(result.reason) is result.should_convert
    assert not analyzer.reason_means_convert("Nur 5% Ersparnis erwartet")
    assert not analyzer.reason_means_convert("")


# --------------------------------------------------------------------------- #
# Learning-model features on the plan
# --------------------------------------------------------------------------- #

@pytest.fixture()
def trained_model(monkeypatch):
    model = predictor.LearnedModel()
    monkeypatch.setattr(predictor, "_model", model)
    # A library where everything comes out 30% bigger than predicted.
    samples = [
        {"features": {"crf": 30 + i % 3, "grain": 0.1 * (i % 4)},
         "predicted_bitrate": 1_000_000, "actual_bitrate": 1_300_000}
        for i in range(40)
    ]
    model.fit(samples, trust_threshold=15)
    return model


def test_sampled_plan_carries_uncorrected_base_and_the_real_features(monkeypatch, tmp_path, trained_model):
    sample = analyzer.SampleResult(ok=True, measured_bitrate=6_000_000, spread=0.1,
                                   segments=3, grain_level=0.42, crf_used=0)
    monkeypatch.setattr(analyzer, "run_samples", AsyncMock(return_value=sample))
    cfg = settings()
    cfg.encoding.auto_film_grain = False
    result = asyncio.run(analyzer.analyze(uhd(), cfg, None, depth="sample", workroot=tmp_path))
    plan = result.plan

    feats = plan.prediction_features
    assert feats["has_sample"] == 1.0
    assert feats["grain"] == pytest.approx(0.42)          # measured, 0..1
    assert set(feats) == set(predictor.FEATURE_KEYS)
    # The base is the prediction before the model's correction ...
    assert plan.base_video_bitrate == pytest.approx(6_000_000 * 0.955, rel=1e-3)
    # ... and the correction applied to exactly these features gives the plan.
    correction, _ = trained_model.correction(feats)
    assert correction > 1.05
    assert plan.predicted_video_bitrate == pytest.approx(plan.base_video_bitrate * correction, rel=1e-3)


def test_quick_plan_has_no_sample_flag(trained_model):
    result = asyncio.run(analyzer.analyze(uhd(), settings(), None, depth="quick"))
    plan = result.plan
    assert plan.prediction_features["has_sample"] == 0.0
    assert plan.prediction_features["grain"] == 0.0
    assert 0 < plan.base_video_bitrate < plan.predicted_video_bitrate


# --------------------------------------------------------------------------- #
# Cancelled scans
# --------------------------------------------------------------------------- #

def test_no_advisor_call_after_cancel():
    advisor = Mock()
    advisor.should_ask = Mock(return_value=True)
    advisor.advise = AsyncMock(return_value=Advice(ok=True))
    cancel = asyncio.Event()
    cancel.set()
    asyncio.run(analyzer.analyze(uhd(), settings(), None, advisor=advisor, depth="quick",
                                 cancel_event=cancel))
    advisor.advise.assert_not_awaited()


def test_trial_encodes_get_the_cancel_event(monkeypatch, tmp_path):
    seen = []

    async def run_with_progress(args, timeout=None, cancel_event=None, **kw):
        seen.append(cancel_event)
        return 1, "abgebrochen"

    monkeypatch.setattr(analyzer.ffmpeg, "run_with_progress", run_with_progress)
    monkeypatch.setattr(analyzer.ffmpeg, "extract_segment", AsyncMock())
    monkeypatch.setattr(analyzer.quality, "measure_grain", AsyncMock(return_value=0.0))
    cancel = asyncio.Event()
    plan = analyzer.planner.build_plan(uhd(), settings(), None)
    asyncio.run(analyzer.run_samples(uhd(), plan, settings(), tmp_path, cancel_event=cancel))
    assert seen and all(e is cancel for e in seen)
