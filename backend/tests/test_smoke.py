"""Smoke tests: the API answers, the predictor behaves, the planner builds sane args."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.gettempdir()) / "optimizarr-pytest"
os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(TMP / "config"))
os.environ.setdefault("OPTIMIZARR_TRANSCODE_DIR", str(TMP / "transcode"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import AppSettings  # noqa: E402
from app.core import planner, predictor  # noqa: E402
from app.core.ffmpeg import MediaInfo  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def make_info(**kw):
    base = dict(
        path="/media/Movies/Example (2019)/Example.mkv", container="matroska",
        size=12 * 1024**3, duration=7200.0, video_codec="h264", width=1920, height=1080,
        fps=23.976, video_bitrate=12_000_000, bit_depth=8, pix_fmt="yuv420p",
        audio_streams=[{"index": 1, "codec": "dts", "channels": 6, "bitrate": 1_500_000,
                        "language": "deu", "default": True, "commentary": False,
                        "channel_layout": "5.1", "sample_rate": 48000, "title": ""}],
        subtitle_streams=[],
    )
    base.update(kw)
    return MediaInfo(**base)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_settings_roundtrip(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["encoding"]["container"] == "mkv"

    r = client.put("/api/settings", json={"encoding": {"crf": 27}, "queue": {"paused": True}})
    assert r.status_code == 200
    assert r.json()["encoding"]["crf"] == 27
    assert r.json()["queue"]["paused"] is True

    # Invalid values must be rejected, not silently stored.
    r = client.put("/api/settings", json={"encoding": {"crf": 999}})
    assert r.status_code == 422

    client.put("/api/settings", json={"encoding": {"crf": 30}, "queue": {"paused": False}})


def test_profile_applies_preset(client):
    r = client.post("/api/settings/profile/space")
    assert r.status_code == 200
    body = r.json()
    assert body["encoding"]["crf"] == 35
    assert body["analysis"]["target_vmaf"] == 91.0
    client.post("/api/settings/profile/balanced")


def test_core_endpoints_answer(client):
    assert client.get("/api/stats").status_code == 200
    assert client.get("/api/jobs").status_code == 200
    assert client.get("/api/files").status_code == 200
    assert client.get("/api/history").status_code == 200
    assert client.get("/api/scan/status").status_code == 200
    assert client.get("/api/library/paths").status_code == 200
    assert client.get("/api/settings/schema").status_code == 200


def test_scan_without_paths_is_rejected(client):
    r = client.post("/api/scan", json={})
    assert r.status_code == 400
    assert "Bibliothekspfade" in r.json()["detail"]


def test_adding_a_nonexistent_library_path_fails(client):
    r = client.post("/api/library/paths", json={"path": "/definitely/not/here"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Prediction logic
# --------------------------------------------------------------------------- #

def test_bloated_source_predicts_large_saving():
    info = make_info(video_bitrate=18_000_000)  # BluRay-ish h264
    inp = predictor.PredictionInput(
        width=info.width, height=info.height, fps=info.fps, duration=info.duration,
        source_bitrate=info.video_bitrate, source_codec="h264", crf=30, preset=6,
        audio_bitrate=192_000,
    )
    out = predictor.predict(inp, source_size=info.size)
    assert out.saving_pct > 40, out.as_dict()
    assert out.size_bytes < info.size


def test_already_lean_source_predicts_no_meaningful_saving():
    # A 1080p HEVC web release at 2 Mbit/s has nothing left to give.
    inp = predictor.PredictionInput(
        width=1920, height=1080, fps=24, duration=7200,
        source_bitrate=2_000_000, source_codec="hevc", crf=30, preset=6,
        audio_bitrate=128_000,
    )
    out = predictor.predict(inp, source_size=int(2_128_000 * 7200 / 8))
    assert out.saving_pct < 20, out.as_dict()


def test_crf_moves_size_in_the_right_direction():
    def size_at(crf):
        inp = predictor.PredictionInput(
            width=1920, height=1080, fps=24, duration=3600,
            source_bitrate=10_000_000, source_codec="h264", crf=crf, preset=6,
        )
        return predictor.predict(inp).size_bytes

    assert size_at(24) > size_at(30) > size_at(38)


def test_higher_resolution_needs_fewer_bits_per_pixel():
    assert predictor.base_bpp(2160) < predictor.base_bpp(1080) < predictor.base_bpp(480)


def test_learned_model_starts_neutral_then_corrects():
    model = predictor.LearnedModel()
    model.fit([], trust_threshold=10)
    assert model.correction({})[0] == 1.0  # cold start = no correction

    # Twenty finished jobs that all came out 30% bigger than predicted.
    features = predictor.build_features(
        predictor.PredictionInput(1920, 1080, 24, 3600, 8_000_000, "h264", crf=30),
        "libsvtav1", False,
    )
    samples = [{
        "features": features,
        "predicted_bitrate": 2_000_000.0,
        "actual_bitrate": 2_600_000.0,
    } for _ in range(20)]
    model.fit(samples, trust_threshold=10)
    factor, _ = model.correction(features)
    assert 1.15 < factor < 1.45, factor


def test_learned_model_ignores_absurd_outliers():
    features = predictor.build_features(
        predictor.PredictionInput(1920, 1080, 24, 3600, 8_000_000, "h264", crf=30),
        "libsvtav1", False,
    )
    samples = [{"features": features, "predicted_bitrate": 2_000_000.0,
                "actual_bitrate": 2_000_000.0} for _ in range(10)]
    samples.append({"features": features, "predicted_bitrate": 2_000_000.0,
                    "actual_bitrate": 500_000_000.0})  # corrupt result
    model = predictor.LearnedModel()
    model.fit(samples, trust_threshold=5)
    assert model.n_samples == 10  # the outlier was dropped
    assert 0.9 < model.correction(features)[0] < 1.1


def test_precheck_skips_av1_and_tiny_files():
    from app.core.analyzer import precheck
    settings = AppSettings()
    skip, reason = precheck(make_info(video_codec="av1"), settings)
    assert skip and "AV1" in reason

    skip, reason = precheck(make_info(size=5 * 1024**2), settings)
    assert skip and "klein" in reason

    skip, reason = precheck(make_info(duration=30.0), settings)
    assert skip

    skip, _ = precheck(make_info(), settings)
    assert not skip


def test_precheck_skips_already_efficient_sources():
    from app.core.analyzer import precheck
    settings = AppSettings()
    lean = make_info(video_codec="hevc", video_bitrate=1_200_000)
    skip, reason = precheck(lean, settings)
    assert skip and "sparsam" in reason


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #

def test_plan_builds_valid_svtav1_command():
    settings = AppSettings()
    info = make_info()
    plan = planner.build_plan(info, settings, hw=None)
    assert plan.encoder == "libsvtav1"        # no hardware -> CPU
    assert plan.pix_fmt == "yuv420p10le"      # force_10bit default

    args = planner.build_ffmpeg_args(plan, info, info.path, "/tmp/out.mkv")
    assert "-c:v" in args and "libsvtav1" in args
    # Qualified with :v so it cannot reach the audio encoders.
    assert "-crf:v" in args
    assert args[-1] == "/tmp/out.mkv"
    # DTS 5.1 at 1.5 Mbit/s is bloated -> Opus
    assert "libopus" in args
    # The channel mapping is left to ffmpeg on purpose - see the next test.
    assert "-mapping_family:a:0" not in args


def test_opus_channel_mapping_is_left_to_ffmpeg():
    """Forcing mapping_family=1 asks for the Vorbis channel order.

    That order only matches layouts which actually use it.  A 5.1(side) track -
    ordinary in web releases - would come out with its surround channels in the
    wrong places, and on some builds refuses to open at all.  Left alone, ffmpeg
    picks the family that fits the layout.
    """
    settings = AppSettings()
    # Bitrates chosen above the "already lean" threshold so every track really
    # gets re-encoded - otherwise the assertion would pass vacuously.
    for layout, channels, bitrate in (
        ("stereo", 2, 448_000),
        ("5.1", 6, 640_000),
        ("5.1(side)", 6, 640_000),
        ("7.1", 8, 1_509_000),
    ):
        info = make_info(audio_streams=[{
            "index": 1, "codec": "eac3", "channels": channels, "bitrate": bitrate,
            "language": "ger", "default": True, "commentary": False,
            "channel_layout": layout, "sample_rate": 48000, "title": "",
        }])
        plan = planner.build_plan(info, settings, hw=None)
        args = planner.build_ffmpeg_args(plan, info, info.path, "/tmp/out.mkv")
        assert "libopus" in args, layout
        assert not any(a.startswith("-mapping_family") for a in args), layout


def test_lean_audio_is_copied_not_reencoded():
    settings = AppSettings()
    info = make_info(audio_streams=[{
        "index": 1, "codec": "aac", "channels": 2, "bitrate": 128_000, "language": "eng",
        "default": True, "commentary": False, "channel_layout": "stereo",
        "sample_rate": 48000, "title": "",
    }])
    plan = planner.build_plan(info, settings, hw=None)
    assert plan.audio[0]["action"] == "copy"


def test_hdr_metadata_is_preserved():
    settings = AppSettings()
    info = make_info(
        is_hdr=True, hdr_format="hdr10", bit_depth=10, pix_fmt="yuv420p10le",
        color_primaries="bt2020", color_transfer="smpte2084", color_space="bt2020nc",
    )
    plan = planner.build_plan(info, settings, hw=None)
    args = planner.build_ffmpeg_args(plan, info, info.path, "/tmp/out.mkv")
    assert "-color_primaries:v" in args and "bt2020" in args
    assert "-color_trc:v" in args and "smpte2084" in args


def test_interlaced_source_gets_deinterlaced():
    settings = AppSettings()
    info = make_info(interlaced=True)
    plan = planner.build_plan(info, settings, hw=None)
    assert plan.deinterlace
    args = planner.build_ffmpeg_args(plan, info, info.path, "/tmp/out.mkv")
    vf = args[args.index("-vf") + 1]
    assert "bwdif" in vf


def test_subtitle_languages_are_filtered():
    settings = AppSettings()
    settings.subtitles.keep_languages = ["deu"]
    info = make_info(subtitle_streams=[
        {"index": 2, "codec": "subrip", "language": "deu", "forced": False,
         "default": True, "text": True, "title": ""},
        {"index": 3, "codec": "subrip", "language": "fra", "forced": False,
         "default": False, "text": True, "title": ""},
    ])
    plan = planner.build_plan(info, settings, hw=None)
    actions = {s["index"]: s["action"] for s in plan.subtitles}
    assert actions[2] == "copy"
    assert actions[3] == "drop"


def test_pgs_subtitles_dropped_for_mp4():
    settings = AppSettings()
    settings.encoding.container = "mp4"
    info = make_info(subtitle_streams=[
        {"index": 2, "codec": "hdmv_pgs_subtitle", "language": "deu", "forced": False,
         "default": True, "text": False, "title": ""},
    ])
    plan = planner.build_plan(info, settings, hw=None)
    assert plan.subtitles[0]["action"] == "drop"


def test_trial_encode_command_is_video_only():
    settings = AppSettings()
    info = make_info()
    plan = planner.build_plan(info, settings, hw=None)
    args = planner.build_ffmpeg_args(
        plan, info, "/tmp/seg.mkv", "/tmp/seg.av1.mkv", quiet_streams=True
    )
    assert "-an" in args and "-sn" in args
    assert "libopus" not in args


def test_sample_positions_avoid_edges():
    positions = planner.sample_positions(6000.0, 3, 0.05)
    assert len(positions) == 3
    assert positions[0] > 300 and positions[-1] < 5700
    assert positions == sorted(positions)


def test_crf_is_clamped_to_configured_range():
    settings = AppSettings()
    settings.encoding.crf_min = 24
    settings.encoding.crf_max = 38
    assert planner.clamp_crf(10, settings) == 24
    assert planner.clamp_crf(50, settings) == 38
    assert planner.clamp_crf(30, settings) == 30


def test_schedule_window_across_midnight():
    import datetime as dt
    from app.core.worker import within_schedule

    settings = AppSettings()
    settings.queue.schedule_enabled = True
    settings.queue.schedule_start = "22:00"
    settings.queue.schedule_end = "07:00"

    monday_2am = dt.datetime(2026, 8, 31, 2, 0)
    monday_11pm = dt.datetime(2026, 8, 31, 23, 0)
    monday_noon = dt.datetime(2026, 8, 31, 12, 0)

    assert within_schedule(settings, monday_2am)[0]
    assert within_schedule(settings, monday_11pm)[0]
    assert not within_schedule(settings, monday_noon)[0]


# --------------------------------------------------------------------------- #
# Quality metric fallback
# --------------------------------------------------------------------------- #

def test_ssim_vmaf_mapping_is_monotonic_and_invertible():
    from app.core.quality import ssim_to_vmaf, vmaf_to_ssim

    ssims = [0.90, 0.94, 0.96, 0.975, 0.985, 0.992, 0.999]
    vmafs = [ssim_to_vmaf(s) for s in ssims]
    assert vmafs == sorted(vmafs), vmafs
    assert all(0 <= v <= 100 for v in vmafs)

    # Round-tripping a target must land close to where it started.
    for target in (85.0, 91.0, 94.0, 96.0):
        assert abs(ssim_to_vmaf(vmaf_to_ssim(target)) - target) < 0.5


def test_ssim_mapping_handles_extremes():
    from app.core.quality import ssim_to_vmaf, vmaf_to_ssim

    assert ssim_to_vmaf(1.0) == 100.0
    assert 0.0 <= ssim_to_vmaf(0.0) <= 70.0
    assert vmaf_to_ssim(100.0) == 1.0
    assert vmaf_to_ssim(0.0) <= 0.9


def test_quality_score_describes_itself():
    from app.core.quality import QualityScore

    exact = QualityScore(value=94.3, metric="vmaf", vmaf_estimate=94.3)
    assert exact.is_exact
    assert exact.describe() == "VMAF 94.3"

    approx = QualityScore(value=0.9812, metric="ssim", vmaf_estimate=94.2)
    assert not approx.is_exact
    assert "SSIM" in approx.describe() and "94" in approx.describe()


def test_grain_level_maps_to_sane_synthesis_values():
    from app.core.quality import grain_synthesis_level

    assert grain_synthesis_level(0.0) == 0       # clean source: never smear it
    assert grain_synthesis_level(0.15) == 0
    assert 4 <= grain_synthesis_level(0.35) <= 12
    assert grain_synthesis_level(1.0) <= 28
    assert grain_synthesis_level(0.8, is_hdr=True) < grain_synthesis_level(0.8)
