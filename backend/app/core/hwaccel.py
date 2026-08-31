"""Intel GPU capability detection.

The container has no idea what silicon it landed on: an Arc A380 can encode AV1
in hardware, a 12th-gen iGPU can only decode it, and a UHD 630 can do neither.
Rather than asking the user to know, we probe at startup:

1. is there a render node at all?
2. what does ``vainfo`` report for profiles/entrypoints?
3. does ffmpeg list the encoder?
4. does a realistic throwaway encode actually succeed?
5. does the full decode-on-GPU path work too?

Only steps 4 and 5 are trusted for "yes, we can use this" - the first three are
for the explanation shown in the UI.

Step 4 runs at the same output format and with the same encoder arguments the
real encode uses.  An earlier version probed 8-bit at a low resolution with
minimal arguments, declared the encoder working, and then watched every real
job fall back to the CPU: a probe that differs from production verifies
nothing.  Step 5 exists because the two halves fail separately - a GPU that
cannot feed itself should lose GPU decoding, not GPU encoding.
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
from .ffmpeg import first_error_line

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
    #: Encoding on the GPU and decoding on the GPU fail independently, so they
    #: get independent verdicts.  None = not probed.
    hw_decode_usable: bool | None = None
    hw_decode_reason: str = ""
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


async def _smoke_test(
    encoder: str, device: str, low_power: bool, pix_fmt: str = "p010le"
) -> tuple[bool, str]:
    """Encode a realistic clip.  The only trustworthy check.

    Deliberately not ten frames of a small 8-bit pattern: a probe whose
    configuration differs from the real encode verifies something that then
    fails in production.  This runs at the output format the encoder will
    actually be asked for, with the same encoder arguments, and for long enough
    that failures which only appear after a few seconds still show up.
    """
    from . import planner  # local import: planner imports this module

    src = ["-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24", "-frames:v", "120"]
    if encoder == "av1_qsv":
        args = [
            "-init_hw_device", f"qsv=hw,child_device={device}",
            "-filter_hw_device", "hw",
            *src,
            "-vf", f"format={pix_fmt},hwupload=extra_hw_frames=64",
            *planner.qsv_encoder_args(30, 6, 120, low_power),
            "-f", "null", "-",
        ]
    elif encoder == "av1_vaapi":
        args = [
            "-vaapi_device", device,
            *src,
            "-vf", f"format={pix_fmt},hwupload",
            "-c:v", "av1_vaapi", "-qp", "30",
            "-f", "null", "-",
        ]
    elif encoder == "hevc_qsv":
        args = [
            "-init_hw_device", f"qsv=hw,child_device={device}",
            "-filter_hw_device", "hw",
            *src,
            "-vf", f"format={pix_fmt},hwupload=extra_hw_frames=64",
            "-c:v", "hevc_qsv", "-global_quality", "28",
            "-f", "null", "-",
        ]
    elif encoder == "libsvtav1":
        sw_fmt = "yuv420p10le" if pix_fmt in ("p010le", "yuv420p10le") else "yuv420p"
        args = [*src, "-vf", f"format={sw_fmt}", "-c:v", "libsvtav1",
                "-crf", "40", "-preset", "12", "-f", "null", "-"]
    else:
        return False, f"unbekannter Encoder {encoder}"

    try:
        code, _, err = await ffmpeg.run_simple(["-y", *args], timeout=180)
    except ffmpeg.FFmpegError as exc:
        return False, str(exc)
    if code == 0:
        return True, ""
    return False, first_error_line(err)


async def _decode_path_test(device: str, low_power: bool, pix_fmt: str) -> tuple[bool, str]:
    """Does the whole decode -> convert -> encode chain hold up?

    Encoding from a generated pattern proves the encoder works; it says nothing
    about decoding on the GPU and handing surfaces straight to the encoder,
    which is where a full-hardware transcode actually tends to break.  So this
    builds a throwaway 8-bit H.264 file and runs the exact command shape the
    planner produces - including the 8-to-10-bit conversion the encoder cannot
    do by itself.

    A failure here disables GPU *decoding* only.  The encoder stays in use, and
    decoding falls back to the CPU, which costs some throughput but keeps the
    expensive half on the GPU.
    """
    import tempfile
    from pathlib import Path as _Path

    from . import planner

    tmp = _Path(tempfile.gettempdir()) / "optimizarr-hwprobe.mp4"
    try:
        code, _, err = await ffmpeg.run_simple([
            "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24",
            "-frames:v", "120", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(tmp),
        ], timeout=120)
        if code != 0:
            return False, "Testdatei konnte nicht erzeugt werden"

        code, _, err = await ffmpeg.run_simple([
            "-y",
            "-init_hw_device", f"qsv=hw,child_device={device}",
            "-filter_hw_device", "hw",
            "-hwaccel", "qsv", "-hwaccel_output_format", "qsv",
            "-hwaccel_device", "hw", "-extra_hw_frames", "16",
            "-i", str(tmp), "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", f"vpp_qsv=format={pix_fmt}",
            *planner.qsv_encoder_args(30, 6, 120, low_power),
            "-f", "null", "-",
        ], timeout=180)
    except ffmpeg.FFmpegError as exc:
        return False, str(exc)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    if code == 0:
        return True, ""
    return False, first_error_line(err)


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

        # --- can the GPU also feed itself? -------------------------------- #
        # Encoding from a generated pattern says nothing about decoding on the
        # GPU and passing surfaces straight to the encoder.  That half fails on
        # its own often enough to be worth a separate verdict: if it does, only
        # GPU decoding is switched off, and the encoder - the expensive half -
        # keeps running.
        if rep.encoders["av1_qsv"].verified:
            ok_decode, decode_reason = await _decode_path_test(device, low_power, "p010le")
            rep.hw_decode_usable = ok_decode
            rep.hw_decode_reason = decode_reason
            if not ok_decode:
                rep.notes.append(
                    "Das Dekodieren auf der GPU funktioniert auf diesem System nicht "
                    f"({decode_reason}). Optimizarr kodiert weiterhin auf der GPU und "
                    "dekodiert auf der CPU - etwas langsamer, aber stabil."
                )
                log.warning("GPU decode path unusable: %s", decode_reason)

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
