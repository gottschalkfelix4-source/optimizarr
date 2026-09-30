"""Run the real API -> scan -> queue -> durable publication flow as uid 99."""
import hashlib
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

assert os.geteuid() == 99


def api(path, method="GET", payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        "http://127.0.0.1:8080/api" + path, data=body, method=method,
        headers={"X-Optimizarr": "1", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def wait_for(function, description):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = function()
        if result:
            return result
        time.sleep(.5)
    raise AssertionError("Timeout: " + description)


source = Path("/media/api-flow.mkv")
output = Path("/media/api-flow.av1.mkv")
assert not source.exists() and not output.exists(), "requires fresh smoke volumes"
subprocess.run([
    "ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
    "testsrc2=size=128x96:rate=12:duration=2", "-c:v", "libx264",
    "-preset", "ultrafast", "-crf", "18", str(source),
], check=True, timeout=30)
original = hashlib.sha256(source.read_bytes()).hexdigest()
api("/settings", "PUT", {
    "queue": {"paused": True, "min_free_disk_gb": 0, "cpu_threads": 2},
    "library": {"min_file_size_mb": 0, "min_duration_seconds": 0, "scan_interval_hours": 0},
    "analysis": {"mode": "quick"}, "hardware": {"hw_encode": False, "hw_decode": False},
    "encoding": {"preset": 12},
    "output": {"mode": "sidecar", "verify_full_decode": True},
})
api("/library/paths", "POST", {"path": "/media", "name": "API smoke"})
api("/scan", "POST", {"depth": "quick"})
scan = wait_for(lambda: api("/scan/status")["last_run"]
                if not api("/scan/status")["live"]["running"] else None, "scan")
assert scan["state"] == "done", scan
files = api("/files")["items"]
file_id = next(row["id"] for row in files if row["path"] == str(source))
assert api("/jobs", "POST", {"file_ids": [file_id], "force": True})["added"] == 1
api("/settings", "PUT", {"queue": {"paused": False}})


def finished_job():
    jobs = api("/jobs?state=all")["items"]
    return next((row for row in jobs if row["file_id"] == file_id
                 and row["state"] not in ("queued", "running")), None)


job = wait_for(finished_job, "encode and DB commit")
assert job["state"] == "done", job
assert original == hashlib.sha256(source.read_bytes()).hexdigest()
assert output.exists() and output.stat().st_uid == 99
assert not list(Path("/config/pending-commits").glob("*.json"))
probe = subprocess.run([
    "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output),
], check=True, capture_output=True, text=True, timeout=10)
assert json.loads(probe.stdout)["streams"][0]["codec_name"] == "av1"
print("API scan, queue, strict decode, sidecar and DB commit passed as uid 99.")
