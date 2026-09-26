"""ffmpeg processes never outlive the call that started them, and a running
encode never stalls on a pipe nobody reads.

These run real processes: real ffmpeg (lavfi sources) where ffmpeg's own
behaviour matters, and a tiny stand-in script where the test needs output
ffmpeg would not produce on demand (huge lines, a SIGTERM it ignores).
"""
import asyncio
import os
import shutil
import signal
import stat
import sys
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.gettempdir()) / "optimizarr-pytest"
os.environ.setdefault("OPTIMIZARR_CONFIG_DIR", str(TMP / "config"))
os.environ.setdefault("OPTIMIZARR_TRANSCODE_DIR", str(TMP / "transcode"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import ffmpeg  # noqa: E402

HAVE_FFMPEG = bool(shutil.which(ffmpeg.FFMPEG) or os.access(ffmpeg.FFMPEG, os.X_OK))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")

#: Runs in real time for minutes unless something stops it.
ENDLESS = ["-re", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25", "-t", "300",
           "-f", "null", "-"]


def _alive(proc) -> bool:
    if proc.returncode is not None:
        return False
    try:
        os.kill(proc.pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def spawned(monkeypatch):
    """Every process the ffmpeg module starts; whatever survives is killed."""
    procs = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        proc = await real(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(ffmpeg.asyncio, "create_subprocess_exec", spy)
    yield procs
    for proc in procs:
        if proc.returncode is None:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def _started(procs, count: int = 1) -> None:
    for _ in range(200):
        if len(procs) >= count:
            await asyncio.sleep(0.3)  # let it get going
            return
        await asyncio.sleep(0.05)
    raise AssertionError("process never started")


def _fake_ffmpeg(tmp_path: Path, body: str) -> str:
    """A stand-in for the ffmpeg binary: ignores its arguments, runs ``body``."""
    script = tmp_path / "fake-ffmpeg"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys, time\n"
        "out = sys.stdout.buffer\n"
        "err = sys.stderr.buffer\n"
        + body
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #

@needs_ffmpeg
def test_cancelled_encode_task_kills_ffmpeg(spawned):
    """Task.cancel() used to leave ffmpeg running with nobody left to stop it."""
    async def scenario():
        task = asyncio.create_task(ffmpeg.run_with_progress(ENDLESS))
        await _started(spawned)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert spawned and not _alive(spawned[0])


@needs_ffmpeg
def test_cancel_event_still_stops_the_encode(spawned):
    async def scenario():
        event = asyncio.Event()
        task = asyncio.create_task(ffmpeg.run_with_progress(ENDLESS, cancel_event=event))
        await _started(spawned)
        event.set()
        return await asyncio.wait_for(task, 15)

    code, _ = asyncio.run(scenario())
    assert code != 0 or not _alive(spawned[0])
    assert not _alive(spawned[0])


def test_sigterm_escalation_is_not_abandoned(tmp_path, monkeypatch, spawned):
    """A process that ignores SIGTERM must still die if the task goes away
    during the grace period, instead of the escalation being cancelled with it."""
    fake = _fake_ffmpeg(tmp_path, (
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(120)\n"
    ))
    monkeypatch.setattr(ffmpeg, "FFMPEG", fake)

    async def scenario():
        event = asyncio.Event()
        task = asyncio.create_task(ffmpeg.run_with_progress([], cancel_event=event))
        await _started(spawned)
        event.set()                 # SIGTERM - ignored
        await asyncio.sleep(0.5)    # inside the 10 s grace period
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not _alive(spawned[0])


@needs_ffmpeg
def test_cancelled_run_simple_task_kills_ffmpeg(spawned):
    async def scenario():
        task = asyncio.create_task(ffmpeg.run_simple(ENDLESS))
        await _started(spawned)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not _alive(spawned[0])


@needs_ffmpeg
def test_run_simple_honours_a_cancel_event(spawned):
    async def scenario():
        event = asyncio.Event()
        task = asyncio.create_task(ffmpeg.run_simple(ENDLESS, cancel_event=event))
        await _started(spawned)
        event.set()
        with pytest.raises(ffmpeg.FFmpegCancelled):
            await asyncio.wait_for(task, 10)

    asyncio.run(scenario())
    assert not _alive(spawned[0])
    # Callers that only know FFmpegError keep working.
    assert issubclass(ffmpeg.FFmpegCancelled, ffmpeg.FFmpegError)


@needs_ffmpeg
def test_run_simple_timeout_kills_ffmpeg(spawned):
    with pytest.raises(ffmpeg.FFmpegError):
        asyncio.run(ffmpeg.run_simple(ENDLESS, timeout=1))
    assert not _alive(spawned[0])


@needs_ffmpeg
def test_normal_runs_are_unaffected(spawned):
    code, out, err = asyncio.run(ffmpeg.run_simple(
        ["-f", "lavfi", "-i", "testsrc2=size=64x64:rate=5", "-frames:v", "3", "-f", "null", "-"]
    ))
    assert code == 0
    seen = []
    code, tail = asyncio.run(ffmpeg.run_with_progress(
        ["-f", "lavfi", "-i", "testsrc2=size=64x64:rate=5", "-frames:v", "3", "-f", "null", "-"],
        on_progress=lambda p: seen.append(p.done),
    ))
    assert code == 0 and seen and seen[-1] is True


# --------------------------------------------------------------------------- #
# Pipe pumps
# --------------------------------------------------------------------------- #

def test_huge_lines_do_not_stall_the_encode(tmp_path, monkeypatch, spawned):
    """asyncio's readline raises on lines over 64 KiB.  That ended the reader,
    the pipe filled, and ffmpeg blocked forever on its next write."""
    fake = _fake_ffmpeg(tmp_path, (
        "err.write(b'x' * 300_000 + b'\\n'); err.flush()\n"
        "out.write(b'y' * 200_000 + b'\\n'); out.flush()\n"
        "for i in range(3000):\n"
        "    err.write(b'[info] filler line %d\\n' % i)\n"
        "for i in range(50):\n"
        "    out.write(b'frame=%d\\nout_time_us=%d\\nprogress=continue\\n' % (i, i * 40000))\n"
        "out.write(b'progress=end\\n'); out.flush()\n"
        "err.write(b'DONE-MARKER\\n'); err.flush()\n"
    ))
    monkeypatch.setattr(ffmpeg, "FFMPEG", fake)
    seen = []

    code, tail = asyncio.run(asyncio.wait_for(
        ffmpeg.run_with_progress([], on_progress=lambda p: seen.append(p.frame)), 30
    ))
    assert code == 0
    assert tail.splitlines()[-1] == "DONE-MARKER"
    assert seen[-1] == 49
    # The monster line is kept only in part.
    assert max(len(line) for line in tail.splitlines()) <= 4000


def test_failing_progress_callback_does_not_stall_the_encode(tmp_path, monkeypatch, spawned):
    """An exception in on_progress used to kill the stdout reader."""
    fake = _fake_ffmpeg(tmp_path, (
        "for i in range(3000):\n"
        "    out.write(b'frame=%d\\nfps=25.0\\nprogress=continue\\n' % i)\n"
        "out.write(b'progress=end\\n'); out.flush()\n"
        "err.write(b'finished\\n')\n"
    ))
    monkeypatch.setattr(ffmpeg, "FFMPEG", fake)
    calls = []

    def broken(progress):
        calls.append(progress.frame)
        raise RuntimeError("database is locked")

    code, tail = asyncio.run(asyncio.wait_for(
        ffmpeg.run_with_progress([], on_progress=broken), 30
    ))
    assert code == 0
    assert len(calls) == 3001          # every block still reported
    assert "finished" in tail


def test_failing_async_callback_is_contained_too(tmp_path, monkeypatch, spawned):
    fake = _fake_ffmpeg(tmp_path, (
        "for i in range(10):\n"
        "    out.write(b'frame=%d\\nprogress=continue\\n' % i)\n"
        "out.write(b'progress=end\\n')\n"
    ))
    monkeypatch.setattr(ffmpeg, "FFMPEG", fake)
    calls = []

    async def broken(progress):
        calls.append(progress.done)
        raise RuntimeError("boom")

    code, _ = asyncio.run(asyncio.wait_for(ffmpeg.run_with_progress([], on_progress=broken), 30))
    assert code == 0 and len(calls) == 11 and calls[-1] is True


def test_read_lines_splits_overlong_lines_instead_of_raising():
    async def scenario():
        reader = asyncio.StreamReader(limit=1024)
        reader.feed_data(b"a" * 5000 + b"\nshort\ntail-without-newline")
        reader.feed_eof()
        return [chunk async for chunk in ffmpeg._read_lines(reader)]

    chunks = asyncio.run(scenario())
    assert b"".join(chunks) == b"a" * 5000 + b"\nshort\ntail-without-newline"
    assert chunks[-2:] == [b"short\n", b"tail-without-newline"]


def test_extract_segment_stops_on_cancel_without_retrying(monkeypatch):
    """A cancelled cut must not fall through to the slower decode-based retry."""
    calls = []

    async def fake(args, timeout=None, cancel_event=None):
        calls.append(cancel_event)
        raise ffmpeg.FFmpegCancelled("abgebrochen", -9)

    monkeypatch.setattr(ffmpeg, "run_simple", fake)
    event = asyncio.Event()
    with pytest.raises(ffmpeg.FFmpegCancelled):
        asyncio.run(ffmpeg.extract_segment("/media/x.mkv", 10, 5, "/tmp/seg.mkv", cancel_event=event))
    assert calls == [event]
