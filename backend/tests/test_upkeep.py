"""WAL-safe backup/restore, metadata retention and independent prediction errors."""
import datetime as dt
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from app import config, db
from app.main import app
from app.core import upkeep, predictor
from app.models import Base, HistoryEntry, Job, LearningSample, MediaFile, ScanRun, utcnow


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "config"
    root.mkdir()
    engine = create_engine(f"sqlite:///{root / 'optimizarr.db'}", connect_args={"check_same_thread":False})
    Base.metadata.create_all(engine)
    with engine.begin() as con:
        con.exec_driver_sql("PRAGMA journal_mode=WAL")
    monkeypatch.setattr(db,"_engine",engine)
    monkeypatch.setattr(db,"_SessionLocal",None)
    monkeypatch.setattr(config,"_cache",None)
    monkeypatch.setattr(config,"CONFIG_DIR",root)
    config.save_settings(config.AppSettings())
    yield TestClient(app)
    engine.dispose()


def test_live_wal_backup_roundtrip_preserves_settings_stats_and_manifests(client, tmp_path):
    with db.session_scope() as s:
        s.add(MediaFile(path="/media/converted.mkv",state="done",size=1000,original_size=2000))
        s.add(HistoryEntry(message="recent WAL commit", category="system"))
    config.update_settings({"security":{"auth_enabled":True,"password":"retained-password"}})
    manifest = config.CONFIG_DIR / "trash-items" / "abc.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"state":"available","source":"/media/old.mkv"}))
    response = client.post("/api/system/backups",auth=("admin","retained-password"))
    assert response.status_code == 200, response.text
    data = response.json()
    path = upkeep.backup_path(data["id"])
    assert os.stat(path).st_mode & 0o077 == 0
    assert client.get(data["download_url"],auth=("admin","retained-password")).content.startswith(b"PK")
    destination = tmp_path / "restored-config"
    upkeep.restore(path,destination)
    assert (destination / "trash-items/abc.json").read_bytes() == manifest.read_bytes()
    with sqlite3.connect(destination / "optimizarr.db") as con:
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert con.execute("SELECT original_size-size FROM media_files").fetchone()[0] == 1000
        assert con.execute("SELECT message FROM history").fetchone()[0] == "recent WAL commit"
        security = json.loads(con.execute("SELECT value FROM settings WHERE key='security'").fetchone()[0])
        assert security["password"].startswith("pbkdf2_sha256$")
    config.update_settings({"security":{"auth_enabled":False,"password":""}})
    assert client.get(data["download_url"]).status_code == 409
    with pytest.raises(ValueError,match="leer"):
        upkeep.restore(path,destination)


def test_backup_refuses_running_jobs_and_keeps_a_bounded_count(client):
    config.update_settings({"maintenance":{"max_backups":1}})
    upkeep.backup()
    newest = upkeep.backup()
    assert len(list(upkeep.backups_dir().glob("*.zip"))) == 1
    assert upkeep.backup_path(newest["id"]).exists()
    with db.session_scope() as s:
        media=MediaFile(path="/media/running.mkv",state="encoding")
        s.add(media)
        s.flush()
        s.add(Job(file_id=media.id,state="running"))
    assert client.post("/api/system/backups").status_code == 409


def test_restore_pauses_automation_and_closes_stale_queue(client, tmp_path):
    with db.session_scope() as s:
        media = MediaFile(path="/media/stale.mkv", state="queued", video_codec="hevc")
        s.add(media)
        s.flush()
        s.add(Job(file_id=media.id, state="queued"))
    config.update_settings({"queue": {"paused": False}, "library": {"scan_on_start": True, "scan_interval_hours": 24}})
    saved = upkeep.backup()
    destination = tmp_path / "restored-config"
    upkeep.restore(upkeep.backup_path(saved["id"]), destination)
    with sqlite3.connect(destination / "optimizarr.db") as con:
        settings = {key: json.loads(value) for key, value in con.execute("SELECT key,value FROM settings")}
        assert settings["queue"]["paused"]
        assert not settings["library"]["scan_on_start"]
        assert settings["library"]["scan_interval_hours"] == 0
        assert con.execute("SELECT state FROM jobs").fetchone()[0] == "cancelled"
        assert con.execute("SELECT state FROM media_files").fetchone()[0] == "probed"


def test_restore_rejects_path_traversal_without_creating_destination(tmp_path):
    path=tmp_path / "bad.zip"
    with zipfile.ZipFile(path,"w") as z:
        z.writestr("optimizarr.db",b"invalid")
        z.writestr("backup-manifest.json",'{"format":1}')
        z.writestr("../outside",b"bad")
    with pytest.raises(ValueError,match="Pfad"):
        upkeep.restore(path,tmp_path / "new-config")
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "new-config").exists()


def test_retention_preserves_converted_savings_active_jobs_and_learning(client):
    old=utcnow()-dt.timedelta(days=400)
    with db.session_scope() as s:
        media=MediaFile(path="/media/converted.mkv",state="done",size=1000,original_size=2000)
        s.add(media)
        s.flush()
        job=Job(file_id=media.id,state="done",finished_at=old,log="old large log")
        s.add(job)
        s.flush()
        s.add(LearningSample(job_id=job.id,features={},actual_bitrate=1000,predicted_bitrate=1200))
        s.add(Job(file_id=media.id,state="queued",created_at=old))
        s.add(HistoryEntry(message="old",created_at=old))
        s.add(HistoryEntry(message="recent"))
        s.add(ScanRun(state="done",finished_at=old))
        s.add(ScanRun(state="running",started_at=old))
    result=client.post("/api/system/maintenance")
    assert result.status_code == 200
    assert result.json()["jobs"] == 1
    with db.session_scope() as s:
        assert s.query(MediaFile).one().saved_bytes == 1000
        assert s.query(Job).one().state == "queued"
        assert s.query(LearningSample).one().job_id is None
        assert s.query(HistoryEntry).one().message == "recent"
        assert s.query(ScanRun).one().state == "running"


def test_prediction_evaluation_uses_applied_values_before_training(client):
    with db.session_scope() as s:
        for encoder,base,applied,actual in [("libsvtav1",1000,2000,2500),("av1_qsv",1000,2000,2000),("av1_vaapi",1000,None,8000)]:
            s.add(LearningSample(features={},encoder=encoder,predicted_bitrate=base,applied_bitrate=applied,actual_bitrate=actual))
    before=client.get("/api/stats/model").json()
    assert before["evaluation"]["samples"] == 2
    assert before["evaluation"]["mean_abs_error_pct"] == 10
    assert before["samples"][0]["predicted_kbps"] == 2
    client.post("/api/system/refit-model")
    assert client.get("/api/stats/model").json()["evaluation"] == before["evaluation"]


def test_secret_backup_requires_login_and_learning_cap_is_applied(client):
    config.update_settings({"advisor":{"api_key":"local-test-key"},"maintenance":{"max_learning_samples":2000}})
    assert client.post("/api/system/backups").status_code == 409
    with db.session_scope() as s:
        s.add_all([LearningSample(features={},actual_bitrate=1000,predicted_bitrate=1000) for _ in range(2100)])
    assert upkeep.prune()["learning_samples"] == 100
    with db.session_scope() as s:
        assert s.query(LearningSample).count() == 2000


def test_refit_removes_stale_training_error_when_data_is_removed():
    model=predictor.LearnedModel()
    model.fit([{"features":{},"predicted_bitrate":1000,"actual_bitrate":actual} for actual in (900,1300,1700)])
    assert model.stats()["mean_abs_error_pct"] > 0
    model.fit([])
    assert not model.trained and model.stats()["mean_abs_error_pct"] == 0
