"""Bounded metadata retention and consistent SQLite/config backups."""
from __future__ import annotations
import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from sqlalchemy import delete, select, update
from .. import config, db
from ..models import HistoryEntry, Job, JobState, LearningSample, OAuthCredential, ScanRun, utcnow
from . import maintenance, scanner, output_files, trash


def prune(settings=None) -> dict:
    cfg = (settings or config.load_settings()).maintenance
    counts = {}
    with db.session_scope() as session:
        for model, days, timestamp, conditions in (
            (HistoryEntry, cfg.history_retention_days, HistoryEntry.created_at, []),
            (ScanRun, cfg.scan_retention_days, ScanRun.finished_at, [ScanRun.state != "running"]),
            (Job, cfg.job_retention_days, Job.finished_at, [Job.state.in_([s.value for s in JobState if s.value not in ("queued", "running")])]),
        ):
            if not days:
                counts[model.__tablename__] = 0
                continue
            cutoff = (utcnow() - dt.timedelta(days=days)).replace(tzinfo=None)
            query = select(model.id).where(timestamp < cutoff, *conditions)
            if model is Job:
                query = query.where(Job.id.not_in(output_files.pending_commits()["jobs"]))
            if model is Job:
                session.execute(update(LearningSample).where(LearningSample.job_id.in_(query)).values(job_id=None))
            counts[model.__tablename__] = session.execute(delete(model).where(model.id.in_(query))).rowcount
        keep = select(LearningSample.id).order_by(LearningSample.created_at.desc(), LearningSample.id.desc()).limit(cfg.max_learning_samples)
        counts["learning_samples"] = session.execute(delete(LearningSample).where(LearningSample.id.not_in(keep))).rowcount
    counts["restored_manifests"] = 0
    if cfg.restored_manifest_retention_days:
        cutoff = (utcnow() - dt.timedelta(days=cfg.restored_manifest_retention_days)).timestamp()
        for path in trash.records_dir().glob("*.json"):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                if entry.get("state") == "restored" and entry.get("reconciled") and path.stat().st_mtime < cutoff:
                    path.unlink()
                    counts["restored_manifests"] += 1
            except (OSError, ValueError):
                continue
    return counts


def backups_dir() -> Path:
    return config.CONFIG_DIR / "backups"


def contains_credentials() -> bool:
    settings = config.load_settings()
    if any(getattr(getattr(settings, group), field) for group, field in config.SECRET_FIELDS):
        return True
    with db.session_scope() as session:
        return session.scalar(select(OAuthCredential.provider).where(
            (OAuthCredential.access_token != "") | (OAuthCredential.refresh_token != "") | (OAuthCredential.id_token != "")
        ).limit(1)) is not None


def _snapshot_credentials(connection) -> bool:
    rows = {key: json.loads(value) for key, value in connection.execute("SELECT key, value FROM settings")}
    if any((rows.get(group) or {}).get(field) for group, field in config.SECRET_FIELDS):
        return True
    return connection.execute("SELECT 1 FROM oauth_credentials WHERE access_token != '' OR refresh_token != '' OR id_token != '' LIMIT 1").fetchone() is not None


def backup_path(backup_id: str) -> Path:
    if len(backup_id) != 32 or any(c not in "0123456789abcdef" for c in backup_id):
        raise ValueError("Ungueltige Sicherungs-ID.")
    path = backups_dir() / f"{backup_id}.zip"
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError("Sicherung nicht gefunden.")
    return path


def backup() -> dict:
    with maintenance.exclusive():
        if scanner.state.running:
            raise ValueError("Bitte den Scan vor der Sicherung beenden.")
        with db.session_scope() as session:
            if session.scalar(select(Job.id).where(Job.state == "running").limit(1)):
                raise ValueError("Bitte laufende Jobs vor der Sicherung beenden.")
        if any(output_files.COMMIT_JOURNAL_DIR.glob("*.json")):
            raise ValueError("Offene Dateijournale zuerst wiederherstellen.")
        directory = backups_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_id = uuid.uuid4().hex
        final = directory / f"{backup_id}.zip"
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=directory) as temp:
            snapshot = Path(temp) / "optimizarr.db"
            connection = db.engine().raw_connection()
            try:
                with closing(sqlite3.connect(snapshot)) as dest:
                    connection.driver_connection.backup(dest)
                    if dest.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise OSError("Datenbank-Sicherung hat die Integritaetspruefung nicht bestanden.")
                    has_credentials = _snapshot_credentials(dest)
            finally:
                connection.close()
            archive = Path(temp) / "backup.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
                output.write(snapshot, "optimizarr.db")
                output.writestr("backup-manifest.json", json.dumps({"format": 1, "created_at": utcnow().isoformat(), "contains_credentials": has_credentials}))
                for file in config.CONFIG_DIR.rglob("*"):
                    rel = file.relative_to(config.CONFIG_DIR)
                    if rel.parts[0] == "backups" or file.name.startswith("optimizarr.db") or file.name == ".runtime.lock":
                        continue
                    if file.is_file() and not file.is_symlink() and file.suffix != ".tmp":
                        output.write(file, rel.as_posix())
            os.chmod(archive, 0o600)
            output_files._fsync_file(archive)
            os.replace(archive, final)
            output_files._fsync_dir(directory)
        cfg = config.load_settings().maintenance
        old = sorted(directory.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        for file in old[cfg.max_backups:]:
            if not file.is_symlink():
                file.unlink()
        return {"id": backup_id, "size": final.stat().st_size, "download_url": f"/api/system/backups/{backup_id}"}


def restore(archive: Path, destination: Path) -> None:
    """Restore into an empty config directory; never overwrite a running setup."""
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Wiederherstellungsziel muss ein neuer oder leerer Ordner sein.")
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if len(names) != len(set(names)) or "optimizarr.db" not in names or "backup-manifest.json" not in names:
            raise ValueError("Keine vollstaendige Optimizarr-Sicherung.")
        if json.loads(package.read("backup-manifest.json")).get("format") != 1:
            raise ValueError("Unbekanntes Sicherungsformat.")
        if sum(item.file_size for item in package.infolist()) > 2 * 1024**3:
            raise ValueError("Sicherung ueberschreitet 2 GiB.")
        for item in package.infolist():
            name = PurePosixPath(item.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in item.filename or ":" in item.filename or not name.parts:
                raise ValueError("Sicherung enthaelt einen ungueltigen Pfad.")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Verknuepfungen in Sicherungen sind nicht erlaubt.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".optimizarr-restore-", dir=destination.parent) as temp:
            stage = Path(temp)
            for item in package.infolist():
                if item.is_dir():
                    continue
                target = stage / item.filename
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with package.open(item) as source, target.open("xb") as out:
                    shutil.copyfileobj(source, out)
                os.chmod(target, 0o600)
            with closing(sqlite3.connect(stage / "optimizarr.db")) as restored:
                if restored.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Sicherungsdatenbank ist beschaedigt.")
                tables = {row[0] for row in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"settings", "jobs", "media_files"}.issubset(tables):
                    raise ValueError("Datenbank gehoert nicht zu Optimizarr.")
                # A config backup cannot roll media files back in time. Never
                # resume stale jobs automatically against today's library.
                now = utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
                restored.execute("UPDATE jobs SET state='cancelled', finished_at=?, error='Aus Sicherung wiederhergestellt; Datei vor neuem Auftrag pruefen.' WHERE state IN ('queued','running')", (now,))
                restored.execute("UPDATE media_files SET state=CASE WHEN ignored THEN 'ignored' WHEN video_codec != '' THEN 'probed' ELSE 'new' END WHERE state IN ('queued','encoding','analyzing')")
                restored.execute("UPDATE scan_runs SET state='cancelled', finished_at=?, error='Aus Sicherung wiederhergestellt' WHERE state='running'", (now,))
                for group, patch in (("queue", {"paused": True}), ("library", {"scan_on_start": False, "scan_interval_hours": 0})):
                    row = restored.execute('SELECT value FROM settings WHERE "key"=?', (group,)).fetchone()
                    values = json.loads(row[0]) if row else {}
                    values.update(patch)
                    restored.execute('INSERT INTO settings ("key",value,updated_at) VALUES (?,?,?) ON CONFLICT("key") DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at', (group,json.dumps(values),now))
                restored.execute("INSERT INTO history (created_at,level,category,message) VALUES (?,'warning','system','Konfiguration wiederhergestellt. Warteschlange pausiert, alte Auftraege geschlossen und automatische Scans deaktiviert; Bibliothek vor Fortsetzung pruefen.')", (now,))
                restored.commit()
                restored.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # Atomic directory publication; no partially restored config is visible.
            if destination.exists():
                destination.rmdir()  # verified empty above; races refuse rather than overwrite
            os.rename(stage, destination)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Optimizarr-Sicherung in neuen Konfigurationsordner wiederherstellen")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup")
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("archive", type=Path)
    restore_parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "backup":
        result = backup()
        print(str(backup_path(result["id"])))
    else:
        restore(args.archive, args.destination)
        print(f"Sicherung geprueft und wiederhergestellt: {args.destination.resolve()}")
