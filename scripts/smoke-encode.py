"""Exercise the shipped encoder, muxer and decoder before publishing an image."""
import json
import subprocess
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="optimizarr-smoke-") as folder:
    output = str(Path(folder) / "sample.mkv")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "testsrc2=size=128x96:rate=12:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libsvtav1", "-preset", "12", "-crf", "40",
        "-svtav1-params", "lp=2", "-c:a", "libopus", "-shortest", output,
    ], check=True, timeout=90)
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", output,
    ], check=True, capture_output=True, text=True, timeout=15)
    data = json.loads(result.stdout)
    codecs = {stream["codec_name"] for stream in data["streams"]}
    assert {"av1", "opus"} <= codecs, codecs
    assert 0.8 <= float(data["format"]["duration"]) <= 1.3, data["format"]
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin",
        "-i", output, "-f", "null", "-",
    ], check=True, timeout=30)
print("AV1/Opus: Encode, Probe und vollstaendiges Dekodieren bestanden.")
