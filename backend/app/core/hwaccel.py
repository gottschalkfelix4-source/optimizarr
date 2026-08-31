"""Intel GPU capability detection.

The container has no idea what silicon it landed on: an Arc A380 can encode AV1
in hardware, a 12th-gen iGPU can only decode it, and a UHD 630 can do neither.
Rather than asking the user to know, we probe at startup:

1. is there a render node at all?
2. what does ``vainfo`` report for profiles/entrypoints?
3. does ffmpeg list the encoder?
4. does a 10-frame throwaway encode actually succeed?

Only step 4 is trusted for "yes, we can use this" - the first three are for the
explanation shown in the UI.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import ffmpeg

log = logging.getLogger(__name__)

VAINFO_CANDIDATES = ["/usr/lib/jellyfin-ffmpeg/vainfo", "/usr/bin/vainfo"]

# PCI device id prefixes -> human readable family.  Not exhaustive, just enough
# to tell the user what Optimizarr found.
_INTEL_FAMILIES: list[tuple[str, str]] = [
    ("0x56", "Intel Arc (Alchemist)"),
    ("0x4f", "Intel Arc (Alchemist)"),
    ("0x7d", "Intel Arc Graphics (Meteor Lake / Core Ultra)"),
    ("0xa7", "Intel Iris Xe (Raptor Lake)"),
    ("0x46", "Intel Iris Xe / UHD (Alder Lake, Gen12)"),
    ("0x4c", "Intel UHD Graphics (Rocket Lake, Gen12)"),
    ("0x9a", "Intel Iris Xe (Tiger Lake, Gen12)"),
    ("0x8a", "Intel Iris Plus (Ice Lake, Gen11)"),
    ("0x3e", "Intel UHD Graphics 630 (Coffee Lake, Gen9.5)"),
    ("0x59", "Intel HD Graphics (Kaby Lake, Gen9.5)"),
    ("0x19", "Intel HD Graphics (Skylake, Gen9)"),
]


@dataclass
class EncoderCapability:
    name: str
    available: bool = False          # ffmpeg knows the encoder
    verified: bool = False           # a real test encode succeeded
    reason: str = ""


@dataclass
class HardwareReport:
    device: str = ""
    device_present: bool = False
    readable: bool = False
    gpu_name: str = "unbekannt"
    driver: str = ""
    vainfo_ok: bool = False
    va_profiles: list[str] = field(default_factory=list)
    encoders: dict[str, EncoderCapability] = field(default_factory=dict)
    decode_av1: bool = False
    decode_hevc: bool = False
    decode_h264: bool = False
    decode_vp9: bool = False
    ffmpeg_version: str = ""
    svt_av1: bool = False
    libvmaf: bool = False
    quality_metric: str = "none"       # vmaf | ssim | none
    recommended_encoder: str = "svt_av1"
    summary: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["encoders"] = {k: asdict(v) for k, v in self.encoders.items()}
        return data

    @property
    def hw_av1_encode(self) -> bool:
        return any(
            cap.verified for name, cap in self.encoders.items() if name in ("av1_qsv", "av1_vaapi")
        )


_report: HardwareReport | None = None
_lock = asyncio.Lock()


def _find_vainfo() -> str | None:
    for c in VAINFO_CANDIDATES:
        if Path(c).exists() and os.access(c, os.X_OK):
            return c
    return shutil.which("vainfo")


def _gpu_name_from_sysfs(device: str) -> str:
    """Read the PCI device id behind a render node and map it to a family."""
    node = Path(device).name  # renderD128
    sys_path = Path("/sys/class/drm") / node / "device"
    try:
        vendor = (sys_path / "vendor").read_text().strip().lower()
        dev_id = (sys_path / "device").read_text().strip().lower()
    except OSError:
        return "unbekannt"
    if vendor != "0x8086":
        return f"Nicht-Intel GPU (Vendor {vendor})"
    for prefix, name in _INTEL_FAMILIES:
        if dev_id.startswith(prefix):
            return f"{name} [{dev_id}]"
    return f"Intel GPU [{dev_id}]"


async def _run_vainfo(device: str) -> tuple[bool, str, list[str], str]:
    """Return (ok, driver_string, profile_entrypoint_lines, raw_output)."""
    vainfo = _find_vainfo()
    if not vainfo:
        return False, "", [], "vainfo nicht installiert"
    env = os.environ.copy()
    env.setdefault("LIBVA_DRIVER_NAME", "iHD")
    try:
        proc = await asyncio.create_subprocess_exec(
            vainfo, "--display", "drm", "--device", device,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
        )
        raw_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (asyncio.TimeoutError, OSError) as exc:
        return False, "", [], f"vainfo fehlgeschlagen: {exc}"
    raw = raw_bytes.decode("utf-8", "replace")
    if proc.returncode != 0:
        return False, "", [], raw[-1500:]

    driver = ""
    profiles: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("vainfo: Driver version:"):
            driver = line.split(":", 2)[-1].strip()
        elif line.startswith("VAProfile"):
            profiles.append(re.sub(r"\s+", " ", line))
    return True, driver, profiles, raw[-4000:]


def _va_supports(profiles: list[str], profile_match: str, entrypoint: str) -> bool:
    for line in profiles:
        if profile_match.lower() in line.lower() and entrypoint.lower() in line.lower():
            return True
    return False


async def _smoke_test(encoder: str, device: str, low_power: bool) -> tuple[bool, str]:
    """Encode ten frames of a test pattern.  The only trustworthy check."""
    src = ["-f", "lavfi", "-i", "testsrc2=size=640x480:rate=30", "-frames:v", "10"]
    if encoder == "av1_qsv":
        args = [
            "-init_hw_device", f"qsv=hw,child_device={device}",
            "-filter_hw_device", "hw",
            *src,
            "-vf", "format=nv12,hwupload=extra_hw_frames=16",
            "-c:v", "av1_qsv", "-global_quality", "30",
        ]
        if low_power:
            args += ["-low_power", "1"]
        args += ["-f", "null", "-"]
    elif encoder == "av1_vaapi":
        args = [
            "-vaapi_device", device,
            *src,
            "-vf", "format=nv12,hwupload",
            "-c:v", "av1_vaapi", "-qp", "30",
            "-f", "null", "-",
        ]
    elif encoder == "hevc_qsv":
        args = [
            "-init_hw_device", f"qsv=hw,child_device={device}",
            "-filter_hw_device", "hw",
            *src,
            "-vf", "format=nv12,hwupload=extra_hw_frames=16",
            "-c:v", "hevc_qsv", "-global_quality", "28",
            "-f", "null", "-",
        ]
    elif encoder == "libsvtav1":
        args = [*src, "-c:v", "libsvtav1", "-crf", "40", "-preset", "12", "-f", "null", "-"]
    else:
        return False, f"unbekannter Encoder {encoder}"

    try:
        code, _, err = await ffmpeg.run_simple(["-y", *args], timeout=90)
    except ffmpeg.FFmpegError as exc:
        return False, str(exc)
    if code == 0:
        return True, ""
    tail = [ln for ln in err.strip().splitlines() if ln.strip()][-3:]
    return False, " | ".join(tail)[:300]


async def detect(device: str = "/dev/dri/renderD128", low_power: bool = True,
                 force: bool = False) -> HardwareReport:
    """Probe the host GPU.  Result is cached until ``force=True``."""
    global _report
    async with _lock:
        if _report is not None and not force and _report.device == device:
            return _report

        rep = HardwareReport(device=device)
        rep.ffmpeg_version = await ffmpeg.version()

        encoders = await ffmpeg.available_encoders(refresh=force)
        filters = await ffmpeg.available_filters(refresh=force)
        rep.libvmaf = "libvmaf" in filters
        rep.quality_metric = "vmaf" if rep.libvmaf else ("ssim" if "ssim" in filters else "none")
        rep.svt_av1 = "libsvtav1" in encoders

        # --- CPU encoder is the guaranteed baseline ---
        cpu_cap = EncoderCapability(name="libsvtav1", available=rep.svt_av1)
        if rep.svt_av1:
            ok, reason = await _smoke_test("libsvtav1", device, low_power)
            cpu_cap.verified, cpu_cap.reason = ok, reason
        else:
            cpu_cap.reason = "ffmpeg wurde ohne libsvtav1 gebaut"
        rep.encoders["libsvtav1"] = cpu_cap

        # --- render node ---
        rep.device_present = Path(device).exists()
        rep.readable = rep.device_present and os.access(device, os.R_OK | os.W_OK)
        if not rep.device_present:
            rep.notes.append(
                f"Kein Render-Node unter {device}. In Unraid muss /dev/dri als Device "
                "durchgereicht werden, sonst laeuft alles auf der CPU."
            )
            rep.recommended_encoder = "svt_av1"
            rep.summary = "Keine Intel-GPU sichtbar - CPU-Encoding mit SVT-AV1."
            _report = rep
            return rep
        if not rep.readable:
            rep.notes.append(
                f"{device} existiert, ist aber nicht beschreibbar. Meist fehlt der Container-User "
                "in der Gruppe 'video'/'render' - in Unraid hilft --group-add mit der render-GID."
            )

        rep.gpu_name = _gpu_name_from_sysfs(device)

        ok, driver, profiles, raw = await _run_vainfo(device)
        rep.vainfo_ok = ok
        rep.driver = driver
        rep.va_profiles = profiles
        if not ok:
            rep.notes.append(f"vainfo lieferte kein Ergebnis: {raw.strip()[-200:]}")

        rep.decode_av1 = _va_supports(profiles, "VAProfileAV1", "VLD")
        rep.decode_hevc = _va_supports(profiles, "VAProfileHEVCMain", "VLD")
        rep.decode_h264 = _va_supports(profiles, "VAProfileH264", "VLD")
        rep.decode_vp9 = _va_supports(profiles, "VAProfileVP9", "VLD")

        va_av1_encode = _va_supports(profiles, "VAProfileAV1", "EncSlice")

        for name in ("av1_qsv", "av1_vaapi", "hevc_qsv"):
            cap = EncoderCapability(name=name, available=name in encoders)
            if not cap.available:
                cap.reason = "Encoder in dieser ffmpeg-Version nicht vorhanden"
            elif not rep.readable:
                cap.reason = "Render-Node nicht zugreifbar"
            elif name.startswith("av1_") and not va_av1_encode and ok:
                cap.reason = "GPU meldet keinen AV1-Encode-Entrypoint (nur Decode)"
            else:
                verified, reason = await _smoke_test(name, device, low_power)
                cap.verified, cap.reason = verified, reason
            rep.encoders[name] = cap

        if rep.encoders["av1_qsv"].verified:
            rep.recommended_encoder = "av1_qsv"
        elif rep.encoders["av1_vaapi"].verified:
            rep.recommended_encoder = "av1_vaapi"
        else:
            rep.recommended_encoder = "svt_av1"

        # --- human summary for the dashboard ---
        if rep.hw_av1_encode:
            rep.summary = (
                f"{rep.gpu_name}: AV1-Encoding in Hardware verfuegbar "
                f"({rep.recommended_encoder}). Schnell und CPU-schonend."
            )
        elif rep.decode_av1 or rep.decode_hevc or rep.decode_h264:
            rep.summary = (
                f"{rep.gpu_name}: kein AV1-Encoder in Hardware. Encoding laeuft mit SVT-AV1 "
                "auf der CPU, das Decoding uebernimmt die GPU."
            )
            rep.notes.append(
                "AV1-Hardware-Encoding gibt es erst ab Intel Arc bzw. Core Ultra (Meteor Lake). "
                "Aeltere iGPUs koennen AV1 nur dekodieren."
            )
        else:
            rep.summary = f"{rep.gpu_name}: keine nutzbare Beschleunigung erkannt - reines CPU-Encoding."

        if not rep.libvmaf:
            rep.notes.append(
                "Dieser ffmpeg-Build enthaelt kein libvmaf. Die Qualitaetsmessung laeuft "
                "stattdessen ueber SSIM und wird zur besseren Lesbarkeit auf die VMAF-Skala "
                "umgerechnet."
            )

        _report = rep
        return rep


def cached() -> HardwareReport | None:
    return _report


def build_decode_args(rep: HardwareReport | None, codec: str, enabled: bool,
                      device: str, for_hw_encoder: bool) -> list[str]:
    """Input-side hwaccel flags, chosen conservatively.

    Hardware decoding is only worth it when the GPU actually supports the source
    codec.  Getting this wrong is the #1 cause of green frames, so anything
    unusual falls back to software decoding.
    """
    if not enabled or rep is None or not rep.readable:
        return []
    codec = (codec or "").lower()
    supported = {
        "h264": rep.decode_h264,
        "hevc": rep.decode_hevc,
        "h265": rep.decode_hevc,
        "vp9": rep.decode_vp9,
        "av1": rep.decode_av1,
    }
    if not supported.get(codec):
        return []
    if for_hw_encoder:
        # Keep frames on the GPU - decode and encode share the same context.
        return ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    # Decode on the GPU, hand plain frames back for the CPU encoder.
    return ["-hwaccel", "vaapi", "-hwaccel_device", device, "-hwaccel_output_format", "nv12"]
