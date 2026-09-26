"""Analyzer follow-ups: cancelled helper processes, the QSV quality search."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(Path(tempfile.gettempdir()) / "optimizarr-pytest/config"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import AppSettings  # noqa: E402
from app.core import analyzer, ffmpeg, planner, quality  # noqa: E402
from app.core.ffmpeg import MediaInfo  # noqa: E402


def settings(**analysis) -> AppSettings:
    cfg = AppSettings()
    for key, value in analysis.items():
        setattr(cfg.analysis, key, value)
    return cfg


def film(**kw) -> MediaInfo:
    values = dict(path="/media/Film (2020)/Film.mkv", size=8 * 1024**3, duration=6000,
                  video_codec="h264", width=1920, height=1080, fps=23.976,
                  video_bitrate=10_000_000, bit_depth=8)
    values.update(kw)
    return MediaInfo(**values)


def cancelled(*a, **kw):
    raise ffmpeg.FFmpegCancelled("abgebrochen", -9)


# --- 2. cancel reaches every helper process -------------------------------------- #

def test_sample_cuts_and_the_grain_probe_get_the_cancel_event(monkeypatch, tmp_path):
    cancel = asyncio.Event()
    cuts, grains = [], []

    async def cut(source, start, duration, dest, **kw):
        cuts.append(kw.get("cancel_event"))

    async def grain(path, **kw):
        grains.append(kw.get("cancel_event"))
        return 0.1

    async def encode(args, timeout=None, cancel_event=None, **kw):
        Path(args[-1]).write_bytes(b"x" * 4096)
        return 0, ""

    monkeypatch.setattr(analyzer.ffmpeg, "extract_segment", cut)
    monkeypatch.setattr(analyzer.quality, "measure_grain", grain)
    monkeypatch.setattr(analyzer.ffmpeg, "run_with_progress", encode)
    monkeypatch.setattr(analyzer.ffmpeg, "probe", AsyncMock(return_value=film(duration=10)))
    plan = planner.build_plan(film(), settings(), None)
    result = asyncio.run(analyzer.run_samples(film(), plan, settings(), tmp_path, cancel_event=cancel))
    assert result.ok
    assert cuts and all(e is cancel for e in cuts)
    assert grains == [cancel]


@pytest.mark.parametrize("where, expected", [
    ("cut", ["cut"]),
    ("grain", ["cut", "grain"]),
    ("encode", ["cut", "grain", "encode"]),
])
def test_a_cancelled_helper_ends_the_samples_as_cancelled(
    monkeypatch, tmp_path, caplog, where, expected
):
    calls = []

    async def cut(source, start, duration, dest, **kw):
        calls.append("cut")
        if where == "cut":
            cancelled()

    async def grain(path, **kw):
        calls.append("grain")
        if where == "grain":
            cancelled()
        return 0.0

    async def encode(args, timeout=None, cancel_event=None, **kw):
        calls.append("encode")
        # ffmpeg stopped through the event: a non-zero exit, event set.
        cancel_event.set()
        return 255, "Exiting normally, received signal 15."

    monkeypatch.setattr(analyzer.ffmpeg, "extract_segment", cut)
    monkeypatch.setattr(analyzer.quality, "measure_grain", grain)
    monkeypatch.setattr(analyzer.ffmpeg, "run_with_progress", encode)
    plan = planner.build_plan(film(), settings(), None)
    result = asyncio.run(analyzer.run_samples(
        film(), plan, settings(sample_count=3), tmp_path, cancel_event=asyncio.Event(),
    ))
    assert not result.ok
    assert result.error == "abgebrochen"
    # Stopped at the cancelled step - nothing after it runs, and a stopped
    # trial encode is no "trial encode failed".
    assert calls == expected
    assert "trial encode failed" not in caplog.text


def test_quality_search_passes_the_cancel_event_and_stops_quietly(monkeypatch, tmp_path):
    cancel = asyncio.Event()
    seen = {}

    async def cut(source, start, duration, dest, **kw):
        seen["cut"] = kw.get("cancel_event")

    async def encode(args, timeout=None, cancel_event=None, **kw):
        return 0, ""

    async def measure(ref, dist, **kw):
        seen["measure"] = kw.get("cancel_event")
        raise ffmpeg.FFmpegCancelled("abgebrochen", -9)

    monkeypatch.setattr(analyzer.quality, "available_metric", AsyncMock(return_value="ssim"))
    monkeypatch.setattr(analyzer.ffmpeg, "extract_segment", cut)
    monkeypatch.setattr(analyzer.ffmpeg, "run_with_progress", encode)
    monkeypatch.setattr(analyzer.ffmpeg, "probe", AsyncMock(return_value=film(duration=10)))
    monkeypatch.setattr(analyzer.quality, "measure_quality", measure)
    plan = planner.build_plan(film(), settings(), None)
    crf, score, notes = asyncio.run(analyzer.search_crf_for_quality(
        film(), plan, settings(vmaf_search_steps=4), tmp_path, cancel,
    ))
    assert seen == {"cut": cancel, "measure": cancel}
    assert crf == plan.crf and score is None
    assert notes == ["Qualitaetssuche abgebrochen."]


def test_a_cancelled_trial_encode_is_not_reported_as_failed(monkeypatch):
    async def encode(args, timeout=None, cancel_event=None, **kw):
        cancel_event.set()
        return 255, "received signal 15"

    monkeypatch.setattr(analyzer.ffmpeg, "run_with_progress", encode)
    plan = planner.build_plan(film(), settings(), None)
    with pytest.raises(ffmpeg.FFmpegCancelled):
        asyncio.run(analyzer._encode_segment(
            plan, film(), "/tmp/seg.mkv", "/tmp/out.mkv", 60, cancel_event=asyncio.Event(),
        ))


# --- 5. QSV: neighbouring CRFs share an ICQ -------------------------------------- #

def _search(monkeypatch, tmp_path, encoder, score_of, start_crf=30, target=95.0, steps=6):
    tried: list[float] = []

    async def cut(source, start, duration, dest, **kw):
        return None

    async def encode_segment(plan, info, segment, dest, timeout, cancel_event=None):
        tried.append(plan.crf)
        return 4096, 10.0

    async def measure(ref, dist, **kw):
        vmaf = score_of(tried[-1])
        return quality.QualityScore(vmaf, "vmaf", vmaf)

    monkeypatch.setattr(analyzer.quality, "available_metric", AsyncMock(return_value="vmaf"))
    monkeypatch.setattr(analyzer.ffmpeg, "extract_segment", cut)
    monkeypatch.setattr(analyzer, "_encode_segment", encode_segment)
    monkeypatch.setattr(analyzer.quality, "measure_quality", measure)
    cfg = settings(target_vmaf=target, vmaf_search_steps=steps)
    plan = planner.build_plan(film(), cfg, None)
    plan.encoder = encoder
    plan.crf = start_crf
    best, score, _ = asyncio.run(analyzer.search_crf_for_quality(film(), plan, cfg, tmp_path))
    return tried, best, score


def test_qsv_search_never_encodes_the_same_icq_twice(monkeypatch, tmp_path):
    def icq_score(crf):
        # Quality follows the ICQ the encoder really gets, not the CRF.
        return 95.5 - 1.0 * (planner.hw_quality(crf, "av1_qsv") - 26)

    tried, best, score = _search(monkeypatch, tmp_path, "av1_qsv", icq_score)
    icqs = [planner.hw_quality(c, "av1_qsv") for c in tried]
    assert len(icqs) == len(set(icqs)), f"CRFs {tried} gave ICQs {icqs}"
    # ICQ 26 meets the target of 95, ICQ 27 does not.
    assert planner.hw_quality(best, "av1_qsv") == 26
    assert score is not None and score.vmaf_estimate >= 95.0


def test_qsv_search_steps_on_until_the_icq_changes(monkeypatch):
    cfg = settings()
    history = [(30.0, 95.5)]
    # CRF 31 is ICQ 26 like CRF 30: the next distinct value is CRF 32 (ICQ 27).
    assert planner.hw_quality(31, "av1_qsv") == planner.hw_quality(30, "av1_qsv")
    nxt = analyzer._next_search_crf(30.0, 0.6, cfg, "av1_qsv", history)
    assert nxt == 32
    # Back towards an ICQ that was measured already: nothing new to learn.
    history.append((32.0, 94.5))
    assert analyzer._next_search_crf(32.0, -1.0, cfg, "av1_qsv", history) == 32.0


def test_other_encoders_keep_single_crf_steps():
    cfg = settings()
    assert analyzer._next_search_crf(30.0, 0.6, cfg, "libsvtav1", [(30.0, 95.5)]) == 31
    assert analyzer._next_search_crf(30.0, 0.6, cfg, "av1_vaapi", [(30.0, 95.5)]) == 31
