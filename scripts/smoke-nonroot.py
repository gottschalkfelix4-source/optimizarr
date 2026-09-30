"""Prove runtime ownership, writable volumes and config export as uid 99."""
import json
import os
import urllib.request
from pathlib import Path

assert os.geteuid() == 99
assert os.getegid() == 100
for directory in ("/config", "/transcode", "/media"):
    test = Path(directory) / "optimizarr-smoke-permissions"
    test.write_text("writable")
    assert test.stat().st_uid == 99
    test.unlink()
with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8080/api/system/backups",method="POST",headers={"X-Optimizarr":"1"}),timeout=20) as response:
    data = json.load(response)
assert (Path("/config/backups") / f'{data["id"]}.zip').stat().st_uid == 99
print("uid 99/gid 100: Volumes, Datenbank und Sicherung schreibbar.")
