"""run_job end to end with a fake ffmpeg: gates, re-planning, bookkeeping."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(Path(tempfile.gettempdir()) / "optimizarr-pytest/config"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db
from app.config import AppSettings
from app.core import encoder, planner, predictor, quality
from app.core.ffmpeg import MediaInfo, Progress
from app.models import Base, FileState, HistoryEntry, Job, JobState, LearningSample, MediaFile


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(encoder, "TRANSCODE_DIR", tmp_path / "transcode")
    monkeypatch.setattr(encoder, "COMMIT_JOURNAL_DIR", tmp_path / "config" / "pending-commits", raising=False)
    monkeypatch.setattr(encoder, "TRASH_ROOTS_FILE", tmp_path / "config" / "trash-roots.json", raising=False)
    yield
    engine.dispose()


AUDIO = [{"index": 1, "codec": "ac3", "channels": 6, "bitrate": 448_000, "language": "deu"}]


def probe_info(path, **kw):
    values = dict(path=str(path), size=1000, duration=1400, width=1920, height=1080, fps=24,
                  video_codec="hevc", video_bitrate=4_000_000, audio_streams=list(AUDIO))
    values.update(kw)
    return MediaInfo(**values)


def make_plan(**kw):
    values = dict(audio=[{"index": 1, "action": "copy", "codec": "ac3"}])
    values.update(kw)
    return planner.EncodePlan(**values)


def make_job(tmp_path, plan=None, markers=None, state=JobState.QUEUED.value, known=True,
             content=b"x" * 1000, media_plan=True):
    source = tmp_path / "lib" / "Film.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    st = os.stat(source)
    plan = plan or make_plan()
    data = {**plan.to_dict(), **(markers or {})}
    with db.session_scope() as s:
        media = MediaFile(path=str(source), video_codec="hevc", state=FileState.QUEUED.value,
                          size=st.st_size if known else 0, mtime=st.st_mtime if known else 0.0,
                          plan=plan.to_dict() if media_plan else None)
        s.add(media)
        s.flush()
        job = Job(file_id=media.id, plan=data, state=state)
        s.add(job)
        s.flush()
        return source, job.id, media.id


def settings(**output):
    cfg = AppSettings()
    cfg.analysis.use_learning_model = False
    cfg.queue.min_free_disk_gb = 0
    cfg.output.verify_vmaf = False
    cfg.output.min_accept_saving_percent = 0
    cfg.output.original_action = "delete"
    cfg.output.set_permissions = False
    for key, value in output.items():
        setattr(cfg.output, key, value)
    return cfg


def fake_encoder(calls, size=500, code=0, log_tail="", on_call=None):
    async def fake(plan, info, dest, job_id, settings, cancel):
        calls.append(planner.EncodePlan.from_dict(plan.to_dict()))
        if on_call:
            result = on_call(len(calls), plan)
            if result is not None:
                return result
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"y" * size)
        return code, log_tail
    return fake


def run(job_id, cfg, cancel=None):
    return asyncio.run(encoder.run_job(job_id, cfg, None, cancel or asyncio.Event()))


def job_row(job_id):
    with db.session_scope() as s:
        return s.get(Job, job_id)


def media_row(file_id):
    with db.session_scope() as s:
        return s.get(MediaFile, file_id)


# --- 1. the original changes during the encode ------------------------------- #

def test_source_modified_during_encode_fails_and_keeps_the_new_original(tmp_path, monkeypatch):
    source, job_id, file_id = make_job(tmp_path)
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=probe_info(source)))
    monkeypatch.setattr(encoder.quality, "verify_output", AsyncMock(return_value=(True, "")))
    calls = []

    def upgrade_release(n, plan):
        source.write_bytes(b"z" * 2000)  # Sonarr drops in a better release

    monkeypatch.setattr(encoder, "_run_encode", fake_encoder(calls, on_call=upgrade_release))
    outcome = run(job_id, settings())
    assert not outcome.ok
    assert job_row(job_id).state == JobState.FAILED.value
    assert "veraendert" in job_row(job_id).error
    assert source.read_bytes() == b"z" * 2000
    assert not [p for p in source.parent.iterdir() if p.name.startswith(".optimizarr")]
    assert not any((tmp_path / "transcode").iterdir())


def test_changed_file_is_replanned_with_its_current_streams(tmp_path, monkeypatch):
    source, job_id, _ = make_job(tmp_path, plan=make_plan(base_video_bitrate=1, prediction_features={"crf": 30.0}),
                                 known=True)
    with db.session_scope() as s:
        s.get(MediaFile, s.get(Job, job_id).file_id).size = 123  # scanned before the change
    new = probe_info(source, audio_streams=[{"index": 2, "codec": "aac", "channels": 2, "language": "deu"}],
                     subtitle_streams=[{"index": 3, "codec": "subrip", "language": "deu"}])
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=new))
    monkeypatch.setattr(encoder.quality, "verify_output", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(encoder, "_commit_output", Mock(return_value=str(source)))
    monkeypatch.setattr(encoder, "_record_success", Mock())
    calls = []
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder(calls))
    assert run(job_id, settings()).ok
    assert [a["index"] for a in calls[0].audio] == [2]
    assert [s["index"] for s in calls[0].subtitles] == [3]
    stored = job_row(job_id)
    assert "veraendert" in stored.log and [a["index"] for a in stored.plan["audio"]] == [2]
    # The old prediction belonged to the old file.
    assert calls[0].base_video_bitrate == 0 and calls[0].prediction_features == {}


def test_stream_layout_mismatch_alone_forces_a_new_plan(tmp_path, monkeypatch):
    source, job_id, _ = make_job(tmp_path)
    new = probe_info(source, audio_streams=AUDIO + [{"index": 2, "codec": "aac", "channels": 2}])
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=new))
    monkeypatch.setattr(encoder.quality, "verify_output", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(encoder, "_commit_output", Mock(return_value=str(source)))
    monkeypatch.setattr(encoder, "_record_success", Mock())
    calls = []
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder(calls))
    run(job_id, settings())
    assert sorted(a["index"] for a in calls[0].audio) == [1, 2]


def test_file_replaced_by_an_av1_version_is_not_encoded_again(tmp_path, monkeypatch):
    source, job_id, file_id = make_job(tmp_path)
    with db.session_scope() as s:
        s.get(MediaFile, file_id).size = 5
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=probe_info(source, video_codec="av1")))
    calls = []
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder(calls))
    outcome = run(job_id, settings())
    assert outcome.rejected and not calls
    assert source.read_bytes() == b"x" * 1000


# --- 12. Dolby Vision at job start ---------------------------------------- #

@pytest.mark.parametrize("hdr_format,transfer,mode,forced,runs", [
    ("dolby_vision_p5", "", "hdr10_fallback", True, False),     # P5: never, not even forced
    ("dolby_vision", "", "hdr10_fallback", True, False),        # unknown profile, no HDR10 base
    ("dolby_vision", "smpte2084", "hdr10_fallback", False, True),
    ("dolby_vision_p8", "smpte2084", "skip", False, False),
    ("dolby_vision_p8", "smpte2084", "skip", True, True),       # forced may
    ("dolby_vision_p7", "smpte2084", "hdr10_fallback", False, True),
])
def test_dolby_vision_is_checked_on_the_fresh_probe(tmp_path, monkeypatch, hdr_format, transfer,
                                                   mode, forced, runs):
    markers = {planner.FORCED: True, planner.RESTORE_STATE: "skipped"} if forced else None
    source, job_id, _ = make_job(tmp_path, markers=markers)
    info = probe_info(source, is_hdr=True, hdr_format=hdr_format, color_transfer=transfer)
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=info))
    monkeypatch.setattr(encoder.quality, "verify_output", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(encoder, "_commit_output", Mock(return_value=str(source)))
    monkeypatch.setattr(encoder, "_record_success", Mock())
    calls = []
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder(calls))
    cfg = settings()
    cfg.analysis.dolby_vision = mode
    outcome = run(job_id, cfg)
    assert bool(calls) is runs
    if not runs:
        assert outcome.rejected
        assert job_row(job_id).state == JobState.REJECTED.value
        assert "Dolby Vision" in job_row(job_id).error
        assert source.read_bytes() == b"x" * 1000


# --- 7. quality gate --------------------------------------------------------- #

def test_unmeasurable_quality_rejects_when_the_gate_is_on(tmp_path, monkeypatch):
    source, job_id, _ = make_job(tmp_path)
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=probe_info(source)))
    monkeypatch.setattr(encoder.quality, "verify_output", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(encoder, "_spot_check_quality", AsyncMock(return_value=None))
    commit = Mock(return_value=str(source))
    monkeypatch.setattr(encoder, "_commit_output", commit)
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder([]))
    outcome = run(job_id, settings(verify_vmaf=True))
    assert outcome.rejected and not commit.called
    assert "nicht gemessen" in job_row(job_id).error


def test_verify_output_is_given_the_plan(tmp_path, monkeypatch):
    source, job_id, _ = make_job(tmp_path)
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=probe_info(source)))
    verify = AsyncMock(return_value=(False, "Ergebnis hat 0 Tonspur(en), geplant waren 1"))
    monkeypatch.setattr(encoder.quality, "verify_output", verify)
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder([]))
    assert run(job_id, settings()).rejected
    assert isinstance(verify.await_args.kwargs["plan"], planner.EncodePlan)


# --- 5./6. cancelling ---------------------------------------------------------- #

def test_cancel_during_the_quality_check_does_not_replace(tmp_path, monkeypatch):
    source, job_id, file_id = make_job(tmp_path)
    cancel = asyncio.Event()

    async def measure(*a):
        cancel.set()
        return quality.QualityScore(99, "vmaf", 99)

    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=probe_info(source)))
    monkeypatch.setattr(encoder.quality, "verify_output", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(encoder, "_spot_check_quality", measure)
    commit = Mock(return_value=str(source))
    monkeypatch.setattr(encoder, "_commit_output", commit)
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder([]))
    run(job_id, settings(verify_vmaf=True), cancel)
    assert not commit.called
    job = job_row(job_id)
    assert job.state == JobState.CANCELLED.value and job.finished_at is not None
    assert media_row(file_id).state == FileState.CANDIDATE.value
    assert source.read_bytes() == b"x" * 1000


def test_cancel_before_the_start_never_encodes(tmp_path, monkeypatch):
    source, job_id, file_id = make_job(tmp_path, state=JobState.RUNNING.value)
    cancel = asyncio.Event()
    cancel.set()
    calls = []
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder(calls))
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=probe_info(source)))
    run(job_id, settings(), cancel)
    assert not calls
    job = job_row(job_id)
    assert job.state == JobState.CANCELLED.value and job.finished_at is not None


def test_a_job_cancelled_before_it_started_stays_cancelled(tmp_path, monkeypatch):
    source, job_id, file_id = make_job(tmp_path, state=JobState.CANCELLED.value)
    with db.session_scope() as s:
        s.get(MediaFile, file_id).state = FileState.CANDIDATE.value
    calls = []
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder(calls))
    outcome = run(job_id, settings())
    assert not outcome.ok and not calls
    assert job_row(job_id).state == JobState.CANCELLED.value
    assert media_row(file_id).state == FileState.CANDIDATE.value


def test_shutdown_puts_the_job_back_into_the_queue(tmp_path, monkeypatch):
    source, job_id, file_id = make_job(tmp_path)
    cancel = asyncio.Event()

    def stop(n, plan):
        cancel.requeue = True
        cancel.set()
        return 255, "Exiting normally, received signal 15."

    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=probe_info(source)))
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder([], on_call=stop))
    outcome = run(job_id, settings(), cancel)
    assert outcome.requeued
    job = job_row(job_id)
    assert job.state == JobState.QUEUED.value
    assert job.progress == 0 and job.started_at is None and job.finished_at is None
    assert "beendet" in job.log
    assert media_row(file_id).state == FileState.QUEUED.value


# --- 8. scratch space -------------------------------------------------------- #

def test_full_scratch_disk_requeues_instead_of_failing(tmp_path, monkeypatch):
    source, job_id, file_id = make_job(tmp_path)
    monkeypatch.setattr(encoder, "_free_space_gb", lambda p: 3.0)
    calls = []
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder(calls))
    cfg = settings()
    cfg.queue.min_free_disk_gb = 20
    outcome = run(job_id, cfg)
    assert outcome.requeued and not calls
    assert job_row(job_id).state == JobState.QUEUED.value
    assert media_row(file_id).state == FileState.QUEUED.value


def test_unknown_free_space_is_not_plenty(tmp_path, monkeypatch):
    def broken(path):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(encoder.shutil, "disk_usage", broken)
    assert encoder._free_space_gb(str(tmp_path)) is None
    cfg = settings()
    cfg.queue.min_free_disk_gb = 20
    assert "nicht ermitteln" in encoder.workdir_space_problem(cfg)


# --- 9. the row describes the new file ------------------------------------- #

def test_success_rereads_the_new_file_into_the_row(tmp_path, monkeypatch):
    source, job_id, file_id = make_job(tmp_path)
    before = probe_info(source, bit_depth=8, pix_fmt="yuv420p", video_bitrate=9_000_000)
    after = probe_info(
        source, video_codec="av1", profile="Main", bit_depth=10, pix_fmt="yuv420p10le",
        video_bitrate=2_000_000, is_hdr=True, hdr_format="hdr10", color_transfer="smpte2084",
        color_primaries="bt2020", color_space="bt2020nc", duration=1399.5,
        audio_streams=[{"index": 1, "codec": "opus", "channels": 6}],
        subtitle_streams=[{"index": 2, "codec": "subrip"}],
    )
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(side_effect=[before, after]))
    monkeypatch.setattr(encoder.quality, "verify_output", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(encoder, "_run_encode", fake_encoder([]))
    assert run(job_id, settings()).ok
    media = media_row(file_id)
    assert (media.video_codec, media.bit_depth, media.pix_fmt) == ("av1", 10, "yuv420p10le")
    assert media.video_bitrate == 2_000_000 and media.profile == "Main"
    assert media.hdr_format == "hdr10" and media.color_transfer == "smpte2084"
    assert media.audio_streams[0]["codec"] == "opus" and media.subtitle_streams
    assert media.duration == 1399.5
    assert media.size == 500 and media.mtime == os.stat(source).st_mtime
    assert media.state == FileState.DONE.value
    # Estimate replaced by the real result - bytes and percent agree.
    assert media.estimated_size == 500
    assert media.estimated_saving_bytes == 500
    assert media.estimated_saving_pct == pytest.approx(50.0)


# --- 11. retries ------------------------------------------------------------- #

def test_cpu_encode_also_retries_with_cpu_decoding(tmp_path, monkeypatch):
    source, job_id, _ = make_job(tmp_path, plan=make_plan(encoder="libsvtav1", hw_decode=True))
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=probe_info(source)))
    monkeypatch.setattr(encoder.quality, "verify_output", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(encoder, "_commit_output", Mock(return_value=str(source)))
    monkeypatch.setattr(encoder, "_record_success", Mock())
    calls = []

    def first_fails(n, plan):
        if plan.hw_decode:
            return 1, "[vist#0:0/h264 @ 0x1] Error while decoding: hwaccel changed\nConversion failed!"

    monkeypatch.setattr(encoder, "_run_encode", fake_encoder(calls, on_call=first_fails))
    assert run(job_id, settings()).ok
    assert [(c.encoder, c.hw_decode) for c in calls] == [("libsvtav1", True), ("libsvtav1", False)]


def test_cpu_fallback_restores_grain_synthesis_and_logs_the_gpu_values(tmp_path, monkeypatch):
    plan = make_plan(encoder="av1_vaapi", pix_fmt="p010le", film_grain=0,
                     prediction_features={"grain": 0.6, "is_hw_encoder": 1.0}, base_video_bitrate=1)
    source, job_id, file_id = make_job(tmp_path, plan=plan)
    monkeypatch.setattr(encoder.ffmpeg, "probe", AsyncMock(return_value=probe_info(source)))
    monkeypatch.setattr(encoder.quality, "verify_output", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(encoder, "_commit_output", Mock(return_value=str(source)))
    monkeypatch.setattr(encoder, "_record_success", Mock())
    calls = []

    def gpu_fails(n, plan):
        if plan.is_hardware:
            return 1, "[av1_vaapi @ 0x1] Failed to initialise VAAPI encoder: Input/output error"

    monkeypatch.setattr(encoder, "_run_encode", fake_encoder(calls, on_call=gpu_fails))
    cfg = settings()
    cfg.encoding.auto_film_grain = True
    cfg.encoding.film_grain_synthesis = 0
    outcome = run(job_id, cfg)
    assert outcome.ok and outcome.fell_back_to_cpu
    assert calls[-1].encoder == "libsvtav1"
    assert calls[-1].film_grain == quality.grain_synthesis_level(0.6) > 0
    with db.session_scope() as s:
        entry = s.query(HistoryEntry).filter(HistoryEntry.level == "warning").one()
        assert entry.detail["encoder"] == "av1_vaapi"
        assert entry.detail["film_grain"] == 0 and entry.detail["pix_fmt"] == "p010le"


# --- 10. learning samples ------------------------------------------------------ #

def _record(plan, fell_back=False, info=None):
    with db.session_scope() as s:
        media = MediaFile(path="/lib/a.mkv")
        s.add(media)
        s.flush()
        job = Job(file_id=media.id, plan=plan.to_dict())
        s.add(job)
        s.flush()
        ids = job.id, media.id
    info = info or probe_info("/lib/a.mkv", duration=1000)
    outcome = encoder.EncodeOutcome(ok=True, input_size=10**9, output_size=250_000_000,
                                    fell_back_to_cpu=fell_back)
    encoder._record_success(ids[0], ids[1], outcome, "/lib/a.mkv", plan, info, settings(mode="sidecar"))
    with db.session_scope() as s:
        return s.query(LearningSample).one_or_none()


def test_learning_sample_uses_the_uncorrected_base_and_the_stored_features():
    features = {"log_pixels": 17.0, "crf": 30.0, "has_sample": 1.0, "is_hw_encoder": 0.0, "grain": 0.2}
    plan = make_plan(predicted_video_bitrate=5_000_000, base_video_bitrate=3_000_000,
                     prediction_features=features)
    sample = _record(plan)
    assert sample.predicted_bitrate == 3_000_000
    assert sample.features == features


def test_legacy_plan_learns_from_a_recomputed_heuristic_pair():
    plan = make_plan(predicted_video_bitrate=5_000_000, crf=32)
    info = probe_info("/lib/a.mkv", duration=1000)
    sample = _record(plan, info=info)
    assert sample.predicted_bitrate != 5_000_000
    inp = predictor.PredictionInput(
        width=info.width, height=info.height, fps=info.fps, duration=info.duration,
        source_bitrate=info.video_bitrate, source_codec=info.video_codec, crf=32, preset=plan.preset,
        audio_bitrate=planner.estimate_audio_bitrate(plan, info),
        overhead_bitrate=planner.estimate_overhead_bitrate(info),
    )
    base, features = predictor.heuristic_training_pair(inp, plan.encoder)
    assert sample.predicted_bitrate == pytest.approx(base)
    assert sample.features == features and features["has_sample"] == 0.0


def test_cpu_fallback_discards_a_gpu_measured_sample():
    plan = make_plan(encoder="libsvtav1", base_video_bitrate=3_000_000,
                     prediction_features={"has_sample": 1.0, "is_hw_encoder": 1.0})
    assert _record(plan, fell_back=True) is None


def test_cpu_fallback_relabels_a_heuristic_sample():
    plan = make_plan(encoder="libsvtav1", base_video_bitrate=3_000_000,
                     prediction_features={"has_sample": 0.0, "is_hw_encoder": 1.0})
    sample = _record(plan, fell_back=True)
    assert sample.features["is_hw_encoder"] == 0.0 and sample.encoder == "libsvtav1"
    assert sample.predicted_bitrate == 3_000_000  # the heuristic base still holds


def test_predictor_exposes_what_the_model_must_be_trained_on():
    inp = predictor.PredictionInput(1920, 1080, 24, 3600, 8_000_000, "h264", crf=30)
    out = predictor.predict(inp, encoder="av1_vaapi", sample_bitrate=2_000_000, use_model=False)
    assert out.base_video_bitrate == pytest.approx(2_000_000 * 0.955)
    assert out.features == predictor.build_features(inp, "av1_vaapi", has_sample=True)


def test_model_trained_on_base_pairs_reproduces_the_actual_bitrate():
    """Fit on (features, base, actual) and apply to the same features: the
    corrected prediction lands on the actual value - no double correction."""
    model = predictor.LearnedModel()
    inp = predictor.PredictionInput(1920, 1080, 24, 3600, 8_000_000, "h264", crf=30)
    heur = predictor.predict(inp, use_model=False)
    samples = [{"features": heur.features, "predicted_bitrate": heur.base_video_bitrate,
                "actual_bitrate": heur.base_video_bitrate * 0.8}] * 20
    model.fit(samples, trust_threshold=5)
    factor, _ = model.correction(heur.features)
    assert heur.base_video_bitrate * factor == pytest.approx(heur.base_video_bitrate * 0.8, rel=0.02)


# --- 13. progress bookkeeping ------------------------------------------------- #

def test_unknown_duration_does_not_report_100_percent(monkeypatch):
    published = []
    monkeypatch.setattr(encoder.bus, "publish", lambda kind, data=None: published.append((kind, data)))

    async def fake_run(args, on_progress=None, **kw):
        on_progress(Progress(out_time=5.0, done=False))
        return 0, ""

    monkeypatch.setattr(encoder.ffmpeg, "run_with_progress", fake_run)
    info = probe_info("/lib/a.mkv", duration=0.0)
    asyncio.run(encoder._run_encode(make_plan(), info, "/tmp/x.mkv", 1, settings(), asyncio.Event()))
    progress = [d["progress"] for k, d in published if k == "job.progress"]
    assert progress == [0.0]


def test_late_progress_write_cannot_undo_a_finished_job():
    with db.session_scope() as s:
        media = MediaFile(path="/lib/a.mkv")
        s.add(media)
        s.flush()
        done = Job(file_id=media.id, state=JobState.DONE.value, progress=1.0)
        running = Job(file_id=media.id, state=JobState.RUNNING.value, progress=0.1)
        s.add_all([done, running])
        s.flush()
        ids = done.id, running.id
    encoder._persist_progress(ids[0], {"progress": 0.97})
    encoder._persist_progress(ids[1], {"progress": 0.5})
    assert job_row(ids[0]).progress == 1.0
    assert job_row(ids[1]).progress == 0.5
