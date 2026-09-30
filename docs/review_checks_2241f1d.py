"""Historical reproductions; pass a separate checkout of commit 2241f1d.

The output documents existing bugs, not successful regression checks. All data
and media used here are synthetic and kept in a dedicated temporary directory.
No network or external AI calls are made.
"""
import asyncio
import datetime as dt
import os
import sys
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else Path(__file__).resolve().parents[1]
revision = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
if not revision.startswith("2241f1d"):
    raise SystemExit("Historical script requires a checkout of 2241f1d; use pytest backend/tests on current code.")
WORK = Path(tempfile.mkdtemp(prefix='optimizarr-review-repro-'))
os.environ['OPTIMIZARR_CONFIG_DIR'] = str(WORK / 'config')
os.environ['OPTIMIZARR_TRANSCODE_DIR'] = str(WORK / 'transcode')
sys.path.insert(0, str(ROOT / 'backend'))
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers
from app import config, db, security
from app.models import Base, MediaFile, ScanRun, Job
from app.core import worker, scanner, output_files, durable, ffmpeg, analyzer, planner, hwaccel

engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
db._engine = engine
db._SessionLocal = None
config.save_settings(config.AppSettings())
WORK.joinpath('transcode').mkdir()

def result(name, value):
    print(name + ': ' + str(value))

# Windows cannot fsync directories. Only that platform primitive is stubbed;
# all application path selection, file moves, journals and recovery run normally.
# Windows also requires a writable descriptor for fsync.
def windows_sync_file(path):
    with open(path, 'rb+') as stream:
        os.fsync(stream.fileno())
with ExitStack() as stack:
    if os.name == 'nt':
        stack.enter_context(patch.object(durable, 'sync_dir', lambda path: None))
        stack.enter_context(patch.object(output_files, '_fsync_file', windows_sync_file))
    source = WORK / 'film.mkv'
    source.write_bytes(b'ORIGINAL')
    temp = WORK / 'transcode' / 'encoded.mkv'
    temp.write_bytes(b'ENCODED')
    settings = config.AppSettings(output={'mode': 'sidecar', 'sidecar_suffix': '', 'set_permissions': False})
    target = output_files._commit_output(str(source), str(temp), planner.EncodePlan(), settings, ffmpeg.MediaInfo(path=str(source)))
    result('B1 sidecar original overwritten', source.read_bytes() == b'ENCODED')
    separate = config.AppSettings(output={'mode': 'separate_dir', 'output_dir': ''})
    result('B1 empty separate output dir equals source', output_files._target_path(str(source), planner.EncodePlan(), separate) == str(source))

    source = WORK / 'another.avi'
    source.write_bytes(b'ORIGINAL')
    target = source.with_suffix('.mkv')
    temp = WORK / 'transcode' / 'unused.mkv'
    temp.write_bytes(b'ENCODED')
    settings = config.AppSettings(output={'set_permissions': False})
    with patch.object(output_files, '_stage_and_replace', side_effect=OSError('simulated disk full')):
        try:
            output_files._commit_output(str(source), str(temp), planner.EncodePlan(), settings, ffmpeg.MediaInfo(path=str(source)))
        except OSError:
            pass
    target.write_bytes(b'EXTERNAL FILE CREATED AFTER FAILURE')
    recovered = output_files.recover_interrupted_commits()
    result('B2 recovery deletes unrelated new target', {'target_exists': target.exists(), 'recovered': recovered})

def broken_security():
    raise OSError('settings unavailable')
middleware = security.SecurityMiddleware(None, broken_security)
result('B3 middleware authorizes without credentials on failure', asyncio.run(middleware._authorized(Headers())))
config.save_settings(config.AppSettings(security={'auth_enabled': True, 'username': 'review', 'password': 'temporary-review-password'}))
with patch.object(db, 'session_scope', side_effect=OSError('database locked')):
    result('B3 settings read error disables configured authentication', config.load_settings(force=True).security.auth_enabled is False)
config.save_settings(config.AppSettings())

scheduler = worker.Scheduler()
class Clock(dt.datetime):
    instant = dt.datetime(2026, 9, 30, 10, tzinfo=dt.timezone.utc)
    @classmethod
    def now(cls, tz=None):
        return cls.instant if tz else cls.instant.replace(tzinfo=None)
with patch.object(worker.dt, 'datetime', Clock):
    first = scheduler._compute_next(config.AppSettings())
    Clock.instant = Clock.instant + dt.timedelta(days=3)
    second = scheduler._compute_next(config.AppSettings())
result('B4 first periodic scan moves forward indefinitely', {'first': first.isoformat(), 'three_days_later': second.isoformat()})

settings = config.AppSettings(queue={'schedule_enabled': True, 'schedule_days': [], 'schedule_start': '00:00', 'schedule_end': '23:59'})
result('B5 empty weekdays allow encoding', worker.within_schedule(settings, dt.datetime(2026, 9, 30, 12))[0])

with db.session_scope() as session:
    ignored = MediaFile(path=str(WORK / 'ignored.mkv'), state='ignored', ignored=True, plan=planner.EncodePlan().to_dict())
    skipped = MediaFile(path=str(WORK / 'skipped.mkv'), state='skipped', plan=planner.EncodePlan().to_dict())
    done = MediaFile(path=str(WORK / 'done.mkv'), state='done', video_codec='av1', original_size=10000, size=5000, converted_at=dt.datetime.now(dt.timezone.utc), plan=planner.EncodePlan().to_dict())
    session.add_all([ignored, skipped, done])
    session.flush()
    ignored_id, skipped_id, done_id = ignored.id, skipped.id, done.id
result('B6 non-forced queue accepts ignored and skipped files', worker.enqueue_files([ignored_id, skipped_id], force=False)[0])

scanner._store_probe(done_id, ffmpeg.MediaInfo(path=str(WORK / 'done.mkv'), video_codec='av1', size=5000))
scanner._store_analysis(done_id, analyzer.AnalysisResult(decision='skip', reason='Already AV1'))
with db.session_scope() as session:
    result('B7 completed file loses done state after analysis', session.get(MediaFile, done_id).state)

async def broken_probe(*args, **kwargs):
    raise RuntimeError('unexpected probe failure')
with patch.object(scanner.hwaccel, 'cached', return_value=hwaccel.HardwareReport()), patch.object(scanner.ffmpeg, 'probe', broken_probe):
    scan_result = asyncio.run(scanner.run_scan(analyze_only_ids=[done_id]))
result('B8 scan reports success after unhandled probe error', scan_result)

result('workdir', WORK)
