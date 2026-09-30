"""Concurrency cases beyond single-operation unit tests."""
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import create_engine
from app import config, db
from app.api import routes_library
from app.core import background, encoder, ffmpeg, scanner, worker
from app.models import Base, Job, MediaFile
from conftest import isolated_app_state


def test_two_dry_runs_use_different_outputs_and_cleanup_only_their_file(tmp_path, monkeypatch):
    with isolated_app_state(tmp_path):
        file_engine=create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}",connect_args={"check_same_thread":False})
        Base.metadata.create_all(file_engine)
        monkeypatch.setattr(db,"_engine",file_engine)
        monkeypatch.setattr(db,"_SessionLocal",None)
        source=tmp_path / "source.mkv"
        source.write_bytes(b"original")
        with db.session_scope() as s:
            media=MediaFile(path=str(source),state="candidate",plan={"encoder":"libsvtav1"})
            s.add(media)
            s.flush()
            file_id=media.id
        monkeypatch.setattr(routes_library,"TRANSCODE_DIR",tmp_path)
        monkeypatch.setattr(ffmpeg,"probe",AsyncMock(return_value=ffmpeg.MediaInfo(path=str(source))))
        monkeypatch.setattr(routes_library.hwaccel,"cached",lambda:SimpleNamespace())
        destinations=[]
        async def scenario():
            both=asyncio.Event()
            async def encode(args,**kwargs):
                path=Path(args[-1])
                path.write_bytes(b"test encode")
                destinations.append(path)
                if len(destinations)==2:
                    both.set()
                await both.wait()
                assert path.exists(), "the other request cleaned up this running encode"
                return 0,""
            monkeypatch.setattr(ffmpeg,"run_with_progress",encode)
            results=await asyncio.gather(routes_library.dry_run(file_id),routes_library.dry_run(file_id))
            assert all(r["ok"] for r in results)
        asyncio.run(scenario())
        assert len(set(destinations))==2
        assert not any(p.exists() for p in destinations)
        assert source.read_bytes()==b"original"
        file_engine.dispose()


def test_shutdown_waits_for_cancelled_disk_worker_and_releases_scan_state(tmp_path, monkeypatch):
    with isolated_app_state(tmp_path):
        monkeypatch.setattr(scanner.hwaccel,"cached",lambda:SimpleNamespace())
        started=threading.Event()
        finished=threading.Event()
        def sync(settings,run_id,cancel):
            started.set()
            cancel.wait(5)
            try:
                scanner._check_cancel(cancel)
            finally:
                finished.set()
        monkeypatch.setattr(scanner,"_sync_disk_to_db",sync)
        async def scenario():
            task=background.spawn(scanner.run_scan(),"test-api-scan")
            assert await asyncio.to_thread(started.wait,2)
            scanner.cancel_scan()
            await background.shutdown(grace=.01)
            assert finished.is_set() and task.done()
            assert not scanner.state.running
        asyncio.run(scenario())


def test_strict_decode_failure_rejects_without_replacing(tmp_path, monkeypatch):
    with isolated_app_state(tmp_path):
        source=tmp_path / "source.mkv"
        source.write_bytes(b"original"*1000)
        with db.session_scope() as s:
            media=MediaFile(path=str(source),state="queued",size=8000,mtime=source.stat().st_mtime,plan={"encoder":"libsvtav1"})
            s.add(media)
            s.flush()
            job=Job(file_id=media.id,state="queued",plan=media.plan)
            s.add(job)
            s.flush()
            job_id=job.id
        cfg=config.AppSettings()
        cfg.queue.min_free_disk_gb=0
        cfg.output.verify_full_decode=True
        monkeypatch.setattr(encoder,"TRANSCODE_DIR",tmp_path)
        monkeypatch.setattr(ffmpeg,"probe",AsyncMock(return_value=ffmpeg.MediaInfo(path=str(source),size=8000,duration=10,video_codec="hevc")))
        monkeypatch.setattr(encoder.quality,"verify_output",AsyncMock(return_value=(True,"")))
        monkeypatch.setattr(ffmpeg,"run_simple",AsyncMock(return_value=(1,"","invalid packet")))
        async def encode(plan,info,dest,*args):
            Path(dest).write_bytes(b"encoded"*400)
            return 0,""
        monkeypatch.setattr(encoder,"_run_encode",encode)
        outcome=asyncio.run(encoder.run_job(job_id,cfg,None,encoder.CancelToken()))
        assert outcome.rejected and "invalid packet" in outcome.reason
        assert source.read_bytes()==b"original"*1000
