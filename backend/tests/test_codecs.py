"""Codec exclusions: spelling, precheck, and what happens to files already analysed."""
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
from app.core import codecs, predictor, scanner  # noqa: E402
from app.core.analyzer import precheck  # noqa: E402
from app.core.ffmpeg import MediaInfo  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import FileState, MediaFile  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def make_info(**kw):
    base = dict(
        path="/media/Movies/Example (2019)/Example.mkv", container="matroska",
        size=12 * 1024**3, duration=7200.0, video_codec="h264", width=1920, height=1080,
        fps=23.976, video_bitrate=12_000_000, bit_depth=8, pix_fmt="yuv420p",
        audio_streams=[], subtitle_streams=[],
    )
    base.update(kw)
    return MediaInfo(**base)


# --------------------------------------------------------------------------- #
# Spelling
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("written", ["hevc", "HEVC", "h265", "H.265", "x265", "hvc1", "hev1"])
def test_every_way_of_writing_hevc_means_hevc(written):
    assert codecs.normalise(written) == "hevc"


@pytest.mark.parametrize(
    "written,expected",
    [
        ("h264", "h264"), ("H.264", "h264"), ("x264", "h264"), ("avc1", "h264"),
        ("av01", "av1"), ("AV1", "av1"),
        ("xvid", "mpeg4"), ("DivX", "mpeg4"), ("mp4v", "mpeg4"),
        ("mpeg2", "mpeg2video"), ("h.262", "mpeg2video"),
        ("vc-1", "vc1"), ("h266", "vvc"),
    ],
)
def test_common_spellings_map_to_the_ffprobe_name(written, expected):
    assert codecs.normalise(written) == expected


def test_unknown_codecs_pass_through_lowercased():
    # A codec this table has never heard of must still be excludable.
    assert codecs.normalise("SomeNewCodec") == "somenewcodec"
    assert codecs.label("rawvideo") == "RAWVIDEO"


def test_labels_are_human_readable():
    assert codecs.label("hevc") == "HEVC / H.265"
    assert codecs.label("x265") == "HEVC / H.265"


def test_exclusion_list_is_deduped_across_spellings():
    assert codecs.normalise_list(["HEVC", "h265", "x265", "av1"]) == ["hevc", "av1"]
    assert codecs.normalise_list(["", "  "]) == []


def test_is_excluded_compares_meaning_not_text():
    assert codecs.is_excluded("hevc", ["x265"])
    assert codecs.is_excluded("hvc1", ["HEVC"])
    assert not codecs.is_excluded("h264", ["hevc"])
    assert not codecs.is_excluded("", ["hevc"])


def test_spellings_cover_the_aliases_a_probe_might_have_stored():
    found = codecs.spellings("hevc")
    assert "hevc" in found and "h265" in found and "hvc1" in found
    assert codecs.spellings("") == []


def test_codec_efficiency_uses_the_same_normalisation():
    assert predictor.codec_efficiency("x265") == predictor.codec_efficiency("hevc")
    assert predictor.codec_efficiency("av01") == predictor.codec_efficiency("av1")


# --------------------------------------------------------------------------- #
# Precheck
# --------------------------------------------------------------------------- #

def test_excluded_codec_is_skipped_whatever_the_user_typed():
    settings = AppSettings()
    settings.analysis.skip_codecs = ["av1", "h265"]   # user spelling
    skip, reason = precheck(make_info(video_codec="hevc"), settings)
    assert skip
    assert "HEVC / H.265" in reason
    assert codecs.EXCLUSION_REASON in reason


def test_excluding_hevc_does_not_claim_it_would_not_save():
    """AV1 genuinely saves nothing over AV1.  Over HEVC it would - the file is
    skipped because the user said so, and the reason has to say that."""
    settings = AppSettings()
    settings.analysis.skip_codecs = ["av1", "hevc"]
    _, hevc_reason = precheck(make_info(video_codec="hevc"), settings)
    _, av1_reason = precheck(make_info(video_codec="av1"), settings)
    assert "nichts sparen" not in hevc_reason
    assert "Bereits AV1" in av1_reason


def test_hevc_is_a_candidate_when_it_is_not_excluded():
    settings = AppSettings()          # default excludes av1 only
    # High bitrate so the "already lean" check does not fire instead.
    skip, _ = precheck(make_info(video_codec="hevc", video_bitrate=14_000_000), settings)
    assert not skip


# --------------------------------------------------------------------------- #
# Applying a changed exclusion list to files that were already analysed
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _clean_test_files():
    """Each test starts from an empty library - counts are asserted globally."""
    def wipe():
        with session_scope() as s:
            s.query(MediaFile).filter(MediaFile.path.like("/media/t/%")).delete(
                synchronize_session=False
            )
    wipe()
    yield
    wipe()


def _add_file(path: str, codec: str, state: str, **kw) -> int:
    with session_scope() as s:
        s.query(MediaFile).filter(MediaFile.path == path).delete()
        row = MediaFile(
            path=path, video_codec=codec, state=state, size=8 * 1024**3,
            width=1920, height=1080, duration=5400.0,
            estimated_size=5 * 1024**3, estimated_saving_bytes=3 * 1024**3,
            estimated_saving_pct=37.5, plan={"encoder": "svt_av1"}, **kw,
        )
        s.add(row)
        s.flush()
        return row.id


def _state_of(file_id: int) -> MediaFile:
    with session_scope() as s:
        return s.get(MediaFile, file_id)


def test_excluding_a_codec_clears_existing_candidates():
    candidate = _add_file("/media/t/hevc-candidate.mkv", "hevc", FileState.CANDIDATE.value)
    other = _add_file("/media/t/h264-candidate.mkv", "h264", FileState.CANDIDATE.value)
    queued = _add_file("/media/t/hevc-queued.mkv", "hevc", FileState.QUEUED.value)

    result = scanner.apply_codec_exclusions(["av1"], ["av1", "hevc"])
    assert result["excluded"] == 1
    assert result["queued_untouched"] == 1

    row = _state_of(candidate)
    assert row.state == FileState.SKIPPED.value
    assert codecs.EXCLUSION_REASON in row.decision_reason
    # The dashboard must stop promising a saving nobody intends to collect.
    assert row.estimated_saving_bytes == 0 and row.estimated_size == 0

    assert _state_of(other).state == FileState.CANDIDATE.value
    assert _state_of(queued).state == FileState.QUEUED.value


def test_lifting_an_exclusion_brings_only_those_files_back():
    excluded = _add_file(
        "/media/t/hevc-excluded.mkv", "hevc", FileState.SKIPPED.value,
        decision_reason=f"HEVC / H.265 {codecs.EXCLUSION_REASON}",
    )
    too_small = _add_file(
        "/media/t/hevc-tiny.mkv", "hevc", FileState.SKIPPED.value,
        decision_reason="Datei ist nur 12 MB - zu klein zum Optimieren.",
    )

    result = scanner.apply_codec_exclusions(["av1", "hevc"], ["av1"])
    assert result["restored"] == 1

    assert _state_of(excluded).state == FileState.PROBED.value
    # Re-analysing a file that is skipped for a real reason would cost trial
    # encodes and reach the same answer.
    assert _state_of(too_small).state == FileState.SKIPPED.value


def test_an_unchanged_list_touches_nothing():
    candidate = _add_file("/media/t/untouched.mkv", "hevc", FileState.CANDIDATE.value)
    result = scanner.apply_codec_exclusions(["av1", "x265"], ["hevc", "AV1"])
    assert result["excluded"] == 0 and result["restored"] == 0
    assert _state_of(candidate).state == FileState.CANDIDATE.value


def test_stored_alias_spellings_are_matched_too():
    row = _add_file("/media/t/av01.mkv", "av01", FileState.CANDIDATE.value)
    scanner.apply_codec_exclusions([], ["av1"])
    assert _state_of(row).state == FileState.SKIPPED.value


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def test_settings_normalise_what_the_user_typed(client):
    r = client.put("/api/settings", json={"analysis": {"skip_codecs": ["AV1", "x265", "H.265"]}})
    assert r.status_code == 200
    assert r.json()["analysis"]["skip_codecs"] == ["av1", "hevc"]
    client.put("/api/settings", json={"analysis": {"skip_codecs": ["av1"]}})


def test_saving_settings_reports_what_it_changed(client):
    file_id = _add_file("/media/t/report.mkv", "hevc", FileState.CANDIDATE.value)
    r = client.put("/api/settings", json={"analysis": {"skip_codecs": ["av1", "hevc"]}})
    assert r.status_code == 200
    applied = r.json()["applied"]["codec_exclusions"]
    assert applied["added"] == ["hevc"]
    assert applied["excluded"] == 1
    assert _state_of(file_id).state == FileState.SKIPPED.value

    r = client.put("/api/settings", json={"analysis": {"skip_codecs": ["av1"]}})
    assert r.json()["applied"]["codec_exclusions"]["restored"] == 1


def test_codec_overview_lists_what_the_library_holds(client):
    _add_file("/media/t/overview-a.mkv", "hevc", FileState.CANDIDATE.value)
    _add_file("/media/t/overview-b.mkv", "h264", FileState.CANDIDATE.value)

    r = client.get("/api/library/codecs")
    assert r.status_code == 200
    body = r.json()
    by_codec = {e["codec"]: e for e in body["items"]}
    assert by_codec["hevc"]["label"] == "HEVC / H.265"
    assert by_codec["hevc"]["files"] == 1
    assert by_codec["h264"]["candidates"] == 1
    # av1 is excluded by default and must be listed even with zero files.
    assert by_codec["av1"]["excluded"] is True
    assert any(k["codec"] == "vp9" for k in body["known"])


def test_a_probed_file_is_re_analysed_even_when_only_changed_files_rescan():
    """Lifting an exclusion leaves files in ``probed``.  If the scan skipped
    those, the setting would only take effect after a full rescan - so
    unfinished work is always picked up, whatever the rescan setting says."""
    import tempfile as _tempfile
    from app.core.scanner import _sync_disk_to_db
    from app.models import LibraryPath, ScanRun

    root = Path(_tempfile.mkdtemp(prefix="optimizarr-scan-"))
    video = root / "Film.mkv"
    video.write_bytes(b"\0" * 1024)

    settings = AppSettings()
    settings.library.min_file_size_mb = 0
    settings.library.rescan_changed_only = True     # the default, and the point

    with session_scope() as s:
        s.query(LibraryPath).filter(LibraryPath.path == str(root)).delete()
        lib = LibraryPath(path=str(root), name="scan-test", enabled=True)
        s.add(lib)
        run = ScanRun(trigger="test", state="running")
        s.add(run)
        s.flush()
        lib_id, run_id = lib.id, run.id
        s.add(MediaFile(
            path=str(video), library_id=lib_id, size=video.stat().st_size,
            mtime=video.stat().st_mtime, video_codec="hevc",
            state=FileState.PROBED.value,
        ))

    try:
        _, _, todo = _sync_disk_to_db(settings, run_id)
        with session_scope() as s:
            row = s.query(MediaFile).filter(MediaFile.path == str(video)).one()
            assert row.id in todo
    finally:
        with session_scope() as s:
            s.query(MediaFile).filter(MediaFile.path == str(video)).delete()
            s.query(LibraryPath).filter(LibraryPath.id == lib_id).delete()
        video.unlink(missing_ok=True)
        root.rmdir()
