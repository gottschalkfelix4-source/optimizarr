"""Synthetic 10k/100k SQLite/API/sync benchmark; no real media or FFmpeg needed.

Run each size in a fresh process on Linux (peak RSS includes caches and sync).
Enumeration/stat costs of actual shares are deliberately excluded from sync.
"""
import argparse
import json
import os
import resource
import sys
import tempfile
import threading
import time
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument("--files",type=int,choices=[10000,100000],required=True)
args=parser.parse_args()
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "backend"))

with tempfile.TemporaryDirectory(prefix="optimizarr-benchmark-") as temp:
    os.environ["OPTIMIZARR_CONFIG_DIR"]=str(Path(temp)/"config")
    os.environ["OPTIMIZARR_TRANSCODE_DIR"]=str(Path(temp)/"transcode")
    from sqlalchemy import insert
    from fastapi.testclient import TestClient
    from app import config, db
    from app.main import app
    from app.models import Base, LibraryPath, MediaFile, ScanRun
    from app.core import scanner

    Base.metadata.create_all(db.engine())
    config.save_settings(config.AppSettings())
    root=Path(temp)/"media"
    root.mkdir()
    with db.session_scope() as s:
        library=LibraryPath(path=str(root),name="Benchmark")
        s.add(library)
        s.flush()
        lib_id=library.id
        s.add(ScanRun(id=1,state="running"))
    def entries():
        for i in range(args.files):
            yield lib_id, str(root / f"Series-{i//100:04d}" / f"Season 01/Series-S01E{i%100:03d}.mkv"), 1024**3, 1.0
    with db.engine().begin() as connection:
        batch=[]
        for _,path,size,mtime in entries():
            batch.append({"path":path,"library_id":lib_id,"state":"candidate","video_codec":"hevc","size":size,"mtime":mtime,"estimated_saving_bytes":size//2,"plan":{"encoder":"libsvtav1","notes":["synthetic"]},"audio_streams":[{"index":1,"codec":"ac3","channels":6}]})
            if len(batch)==1000:
                connection.execute(insert(MediaFile),batch)
                batch=[]
        if batch:
            connection.execute(insert(MediaFile),batch)
    client=TestClient(app)
    result={"files":args.files,"kind":"synthetic Linux SQLite; enumeration/stat excluded"}
    for label,url in [("files_page","/api/files?page_size=50"),("series_cold","/api/series?page_size=50"),("series_warm","/api/series?page_size=50"),("movies_cold","/api/movies?page_size=50")]:
        start=time.perf_counter()
        response=client.get(url)
        assert response.status_code==200,response.text
        result[label+"_ms"]=round((time.perf_counter()-start)*1000,2)
    def synthetic_walk(roots,settings,report=None,cancel=None):
        for entry in entries():
            scanner._check_cancel(cancel)
            yield entry
        if report is not None:
            report.found[lib_id]=args.files
    scanner.walk_paths=synthetic_walk
    start=time.perf_counter()
    seen,_,_=scanner._sync_disk_to_db(config.load_settings(),1)
    assert seen==args.files
    result["sync_seconds"]=round(time.perf_counter()-start,3)
    stop=threading.Event()
    ready=threading.Event()
    finished=threading.Event()
    def cancellable_walk(*params,**kwargs):
        ready.set()
        while not stop.is_set():
            time.sleep(.001)
        scanner._check_cancel(stop)
        yield from ()
    scanner.walk_paths=cancellable_walk
    def sync():
        try:
            scanner._sync_disk_to_db(config.load_settings(),1,stop)
        except scanner.ScanCancelled:
            pass
        finally:
            finished.set()
    thread=threading.Thread(target=sync)
    thread.start()
    ready.wait(5)
    start=time.perf_counter()
    stop.set()
    assert finished.wait(2),"cooperative cancellation exceeded 2 seconds"
    result["cancel_ms"]=round((time.perf_counter()-start)*1000,2)
    thread.join()
    result["peak_rss_mib"]=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1)
    print(json.dumps(result))
