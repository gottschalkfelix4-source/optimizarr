"""Turn an analysis result into a concrete ffmpeg command line.

The plan is stored as JSON on the file/job so the UI can show exactly what will
happen, and so a queued job survives a container restart.
"""
from __future__ import annotations

import math
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import AppSettings
from .ffmpeg import MediaInfo
from .hwaccel import HardwareReport

# SVT-AV1 preset (0 slow .. 13 fast) mapped onto the 1..7 scale QSV uses.
_QSV_PRESET_MAP = {
    0: 1, 1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 4, 7: 4, 8: 5, 9: 5, 10: 6, 11: 6, 12: 7, 13: 7,
}

# Subtitle codecs that survive a remux into mp4.
_MP4_SAFE_SUBS = {"mov_text", "subrip", "text"}


@dataclass
class AudioAction:
    index: int
    action: str            # copy | opus | drop
    codec: str = ""
    channels: int = 2
    bitrate: int = 0       # target bits/s when re-encoding
    language: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SubtitleAction:
    index: int
    action: str            # copy | drop
    codec: str = ""
    language: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EncodePlan:
    """Everything needed to run and to explain one conversion."""

    encoder: str = "libsvtav1"
    crf: float = 30.0
    preset: int = 6
    pix_fmt: str = "yuv420p10le"
    film_grain: int = 0
    film_grain_denoise: int = 0
    tune: int = 0
    target_height: int = 0          # 0 = keep source
    deinterlace: bool = False
    hw_decode: bool = False
    hw_device: str = ""
    low_power: bool = True
    keyint_frames: int = 240
    container: str = "mkv"
    audio: list[dict[str, Any]] = field(default_factory=list)
    subtitles: list[dict[str, Any]] = field(default_factory=list)
    copy_chapters: bool = True
    copy_attachments: bool = True
    extra_args: str = ""
    threads: int = 0
    # --- informational, shown in the UI ---
    estimated_size: int = 0
    estimated_saving_bytes: int = 0
    estimated_saving_pct: float = 0.0
    predicted_video_bitrate: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EncodePlan | None":
        if not data:
            return None
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @property
    def is_hardware(self) -> bool:
        return self.encoder in ("av1_qsv", "av1_vaapi")

    def describe(self) -> str:
        """One-line human summary for the UI."""
        bits = [f"{self.encoder}", f"CRF {self.crf:g}"]
        if self.encoder == "libsvtav1":
            bits.append(f"Preset {self.preset}")
        if self.pix_fmt.endswith("10le"):
            bits.append("10-bit")
        if self.film_grain:
            bits.append(f"Filmkorn {self.film_grain}")
        if self.target_height:
            bits.append(f"auf {self.target_height}p skaliert")
        if self.deinterlace:
            bits.append("deinterlaced")
        transcoded = sum(1 for a in self.audio if a.get("action") == "opus")
        dropped = sum(1 for a in self.audio if a.get("action") == "drop")
        if transcoded:
            bits.append(f"{transcoded}x Audio zu Opus")
        if dropped:
            bits.append(f"{dropped}x Audio entfernt")
        return ", ".join(bits)


# --------------------------------------------------------------------------- #
# Stream planning
# --------------------------------------------------------------------------- #

def plan_audio(info: MediaInfo, settings: AppSettings) -> tuple[list[AudioAction], int]:
    """Decide per audio track, and return the resulting total audio bitrate."""
    cfg = settings.audio
    actions: list[AudioAction] = []
    keep_langs = {l.strip().lower() for l in cfg.keep_languages if l.strip()}
    total_bitrate = 0

    for stream in info.audio_streams:
        idx = stream["index"]
        lang = (stream.get("language") or "und").lower()
        channels = max(1, int(stream.get("channels") or 2))
        codec = stream.get("codec", "")
        src_bitrate = int(stream.get("bitrate") or 0)
        is_default = bool(stream.get("default"))

        # --- drop rules ---
        if keep_langs and lang not in keep_langs and lang != "und":
            if not (is_default and cfg.keep_default_track_always):
                actions.append(AudioAction(idx, "drop", codec, channels, 0, lang,
                                           f"Sprache {lang} nicht in der Auswahl"))
                continue
        if cfg.drop_commentary and stream.get("commentary"):
            if not (is_default and cfg.keep_default_track_always):
                actions.append(AudioAction(idx, "drop", codec, channels, 0, lang, "Kommentarspur"))
                continue

        # --- lossless sources are always worth re-encoding ---
        lossless = codec in ("truehd", "flac", "pcm_s16le", "pcm_s24le", "mlp", "dts")
        target = channels * cfg.opus_bitrate_per_channel * 1000
        # Opus does not benefit from more than ~256k even on 7.1
        target = min(target, 510_000)

        if cfg.mode == "copy":
            actions.append(AudioAction(idx, "copy", codec, channels, src_bitrate, lang, "Audio wird kopiert"))
            total_bitrate += src_bitrate or channels * 96_000
            continue

        if cfg.mode == "opus":
            if codec == "opus":
                actions.append(AudioAction(idx, "copy", codec, channels, src_bitrate, lang,
                                           "bereits Opus"))
                total_bitrate += src_bitrate or target
            else:
                actions.append(AudioAction(idx, "opus", codec, channels, target, lang,
                                           "Umwandlung nach Opus"))
                total_bitrate += target
            continue

        # cfg.mode == "opus_if_bloated"
        threshold = channels * cfg.bloat_threshold_kbps_per_channel * 1000
        effective = src_bitrate or (channels * 128_000 if lossless else 0)
        if codec == "opus":
            actions.append(AudioAction(idx, "copy", codec, channels, src_bitrate, lang, "bereits Opus"))
            total_bitrate += src_bitrate or target
        elif lossless or (effective and effective > threshold):
            saved = max(0, effective - target)
            reason = (
                f"{codec.upper()} mit {effective//1000} kbit/s -> Opus {target//1000} kbit/s"
                f" (spart {saved//1000} kbit/s)"
            )
            actions.append(AudioAction(idx, "opus", codec, channels, target, lang, reason))
            total_bitrate += target
        else:
            actions.append(AudioAction(idx, "copy", codec, channels, src_bitrate, lang,
                                       "bereits sparsam - wird kopiert"))
            total_bitrate += src_bitrate or channels * 96_000

    return actions, total_bitrate


def plan_subtitles(info: MediaInfo, settings: AppSettings, container: str) -> list[SubtitleAction]:
    cfg = settings.subtitles
    actions: list[SubtitleAction] = []
    keep_langs = {l.strip().lower() for l in cfg.keep_languages if l.strip()}

    for stream in info.subtitle_streams:
        idx = stream["index"]
        lang = (stream.get("language") or "und").lower()
        codec = stream.get("codec", "")
        if cfg.mode == "drop":
            actions.append(SubtitleAction(idx, "drop", codec, lang))
            continue
        if cfg.mode == "text_only" and not stream.get("text"):
            actions.append(SubtitleAction(idx, "drop", codec, lang))
            continue
        if keep_langs and lang not in keep_langs and lang != "und" and not stream.get("forced"):
            actions.append(SubtitleAction(idx, "drop", codec, lang))
            continue
        if container == "mp4" and codec not in _MP4_SAFE_SUBS:
            actions.append(SubtitleAction(idx, "drop", codec, lang))
            continue
        actions.append(SubtitleAction(idx, "copy", codec, lang))
    return actions


def choose_encoder(settings: AppSettings, hw: HardwareReport | None) -> tuple[str, list[str]]:
    """Pick the encoder, explaining the choice."""
    notes: list[str] = []
    want = settings.encoding.encoder

    if want != "auto":
        if want == "svt_av1":
            return "libsvtav1", ["Encoder manuell auf SVT-AV1 (CPU) gesetzt"]
        if hw and hw.encoders.get(want) and not hw.encoders[want].verified:
            reason = hw.encoders[want].reason or "Test-Encode fehlgeschlagen"
            if settings.hardware.fallback_to_cpu:
                notes.append(f"{want} nicht nutzbar ({reason}) - Rueckfall auf SVT-AV1")
                return "libsvtav1", notes
            notes.append(f"Warnung: {want} wurde erzwungen, ist aber nicht verifiziert ({reason})")
        return want, notes

    if not settings.hardware.hw_encode:
        return "libsvtav1", ["Hardware-Encoding in den Einstellungen deaktiviert"]
    if hw is None:
        return "libsvtav1", ["Hardware noch nicht erkannt - CPU-Encoding"]
    if hw.encoders.get("av1_qsv") and hw.encoders["av1_qsv"].verified:
        return "av1_qsv", [f"Intel QSV AV1 verfuegbar ({hw.gpu_name})"]
    if hw.encoders.get("av1_vaapi") and hw.encoders["av1_vaapi"].verified:
        return "av1_vaapi", [f"Intel VAAPI AV1 verfuegbar ({hw.gpu_name})"]
    return "libsvtav1", [
        "GPU kann AV1 nicht in Hardware encodieren - SVT-AV1 auf der CPU"
    ]


def build_plan(
    info: MediaInfo,
    settings: AppSettings,
    hw: HardwareReport | None,
    crf: float | None = None,
    film_grain: int | None = None,
    encoder: str | None = None,
) -> EncodePlan:
    """Assemble the full plan for one file."""
    enc, enc_notes = (encoder, []) if encoder else choose_encoder(settings, hw)
    cfg = settings.encoding
    plan = EncodePlan(encoder=enc, notes=list(enc_notes))

    plan.crf = float(crf if crf is not None else cfg.crf)
    plan.preset = cfg.preset
    plan.container = cfg.container
    plan.tune = 0
    plan.threads = settings.queue.cpu_threads
    plan.extra_args = cfg.extra_ffmpeg_args
    plan.copy_chapters = cfg.copy_chapters
    plan.copy_attachments = cfg.copy_attachments and cfg.container == "mkv"

    # --- pixel format ---
    wants_10bit = cfg.force_10bit or info.bit_depth >= 10 or info.is_hdr
    if plan.is_hardware:
        plan.pix_fmt = "p010le" if wants_10bit else "nv12"
    else:
        plan.pix_fmt = "yuv420p10le" if wants_10bit else "yuv420p"
    if wants_10bit and info.bit_depth < 10:
        plan.notes.append("Encoding in 10 Bit - komprimiert auch 8-Bit-Quellen effizienter")

    # --- resolution cap ---
    if cfg.max_width and info.width > cfg.max_width and info.height:
        plan.target_height = int(round(info.height * cfg.max_width / info.width / 2) * 2)
        plan.notes.append(f"Downscale von {info.width}x{info.height} auf Breite {cfg.max_width}")

    # --- interlacing ---
    plan.deinterlace = bool(cfg.deinterlace and info.interlaced)
    if plan.deinterlace:
        plan.notes.append("Interlaced-Quelle wird deinterlaced")

    # --- film grain synthesis ---
    if film_grain is not None:
        plan.film_grain = int(film_grain)
    elif cfg.film_grain_synthesis:
        plan.film_grain = cfg.film_grain_synthesis
    if plan.film_grain and plan.is_hardware:
        plan.notes.append("Filmkorn-Synthese wird nur von SVT-AV1 unterstuetzt - im Hardware-Encoder ignoriert")
        plan.film_grain = 0

    # --- keyframes ---
    fps = info.fps if info.fps > 0 else 24.0
    plan.keyint_frames = max(24, int(round(fps * cfg.keyframe_interval_seconds)))

    # --- hardware decode ---
    plan.hw_device = settings.hardware.render_device
    plan.low_power = settings.hardware.qsv_low_power
    plan.hw_decode = bool(
        settings.hardware.hw_decode
        and hw is not None
        and hw.readable
        and {"h264": hw.decode_h264, "hevc": hw.decode_hevc,
             "vp9": hw.decode_vp9, "av1": hw.decode_av1}.get(info.video_codec.lower(), False)
    )
    if plan.hw_decode and not plan.is_hardware and plan.deinterlace:
        # Mixing VAAPI decode with a software deinterlacer means a download per
        # frame; not worth the complexity, so decode in software instead.
        plan.hw_decode = False

    audio_actions, _ = plan_audio(info, settings)
    plan.audio = [a.to_dict() for a in audio_actions]
    plan.subtitles = [s.to_dict() for s in plan_subtitles(info, settings, plan.container)]
    return plan


# --------------------------------------------------------------------------- #
# Command construction
# --------------------------------------------------------------------------- #

def _video_filters(plan: EncodePlan, info: MediaInfo) -> list[str]:
    """Filter chain for software encoding paths."""
    filters: list[str] = []
    if plan.deinterlace:
        filters.append("bwdif=mode=send_frame:deint=interlaced")
    if plan.target_height:
        filters.append(f"scale=-2:{plan.target_height}:flags=lanczos")
    if not plan.is_hardware:
        filters.append(f"format={plan.pix_fmt}")
    return filters


def _hw_filters(plan: EncodePlan, info: MediaInfo, hw_frames_in: bool) -> list[str]:
    """Filter chain when the encoder lives on the GPU."""
    filters: list[str] = []
    if plan.encoder == "av1_qsv":
        vpp: list[str] = []
        if plan.deinterlace:
            vpp.append("deinterlace=2")
        if plan.target_height:
            vpp.append(f"w=-1:h={plan.target_height}")
        if vpp:
            filters.append("vpp_qsv=" + ":".join(vpp))
        if not hw_frames_in:
            filters.append(f"format={plan.pix_fmt}")
            filters.append("hwupload=extra_hw_frames=64")
    else:  # av1_vaapi
        if hw_frames_in:
            if plan.deinterlace:
                filters.append("deinterlace_vaapi=mode=default")
            if plan.target_height:
                filters.append(f"scale_vaapi=w=-1:h={plan.target_height}:format={plan.pix_fmt}")
        else:
            if plan.deinterlace:
                filters.append("bwdif=mode=send_frame")
            if plan.target_height:
                filters.append(f"scale=-2:{plan.target_height}:flags=lanczos")
            filters.append(f"format={plan.pix_fmt}")
            filters.append("hwupload")
    return filters


def _svtav1_params(plan: EncodePlan) -> str:
    params = [f"tune={plan.tune}"]
    if plan.film_grain:
        params.append(f"film-grain={plan.film_grain}")
        params.append(f"film-grain-denoise={plan.film_grain_denoise}")
    if plan.threads:
        params.append(f"lp={plan.threads}")
    return ":".join(params)


def build_ffmpeg_args(
    plan: EncodePlan,
    info: MediaInfo,
    source: str,
    dest: str,
    duration_limit: float | None = None,
    start_offset: float | None = None,
    quiet_streams: bool = False,
) -> list[str]:
    """Full argument list (without the ffmpeg binary itself).

    ``quiet_streams`` builds a video-only command, used for trial encodes.
    """
    args: list[str] = ["-y"]

    hw_frames_in = False
    if plan.encoder == "av1_qsv":
        args += ["-init_hw_device", f"qsv=hw,child_device={plan.hw_device}", "-filter_hw_device", "hw"]
        if plan.hw_decode:
            args += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-hwaccel_device", "hw"]
            hw_frames_in = True
    elif plan.encoder == "av1_vaapi":
        args += ["-vaapi_device", plan.hw_device]
        if plan.hw_decode:
            args += ["-hwaccel", "vaapi", "-hwaccel_device", plan.hw_device,
                     "-hwaccel_output_format", "vaapi"]
            hw_frames_in = True
    elif plan.hw_decode:
        args += ["-hwaccel", "vaapi", "-hwaccel_device", plan.hw_device,
                 "-hwaccel_output_format", "nv12"]

    if start_offset:
        args += ["-ss", f"{start_offset:.3f}"]
    args += ["-i", source]
    if duration_limit:
        args += ["-t", f"{duration_limit:.3f}"]

    # --- filters ---
    if plan.is_hardware:
        filters = _hw_filters(plan, info, hw_frames_in)
    else:
        if hw_frames_in:
            # Frames arrive in GPU memory but the encoder is on the CPU.
            filters = ["hwdownload", f"format={plan.pix_fmt}"]
            if plan.target_height:
                filters.insert(1, f"scale=-2:{plan.target_height}:flags=lanczos")
        else:
            filters = _video_filters(plan, info)
    if filters:
        args += ["-vf", ",".join(filters)]

    # --- stream mapping ---
    args += ["-map", "0:v:0"]
    if not quiet_streams:
        for a in plan.audio:
            if a.get("action") != "drop":
                args += ["-map", f"0:{a['index']}"]
        for s in plan.subtitles:
            if s.get("action") != "drop":
                args += ["-map", f"0:{s['index']}"]
        if plan.copy_attachments and info.attachments:
            args += ["-map", "0:t?"]
    else:
        args += ["-an", "-sn", "-dn"]

    # --- video encoder ---
    if plan.encoder == "libsvtav1":
        args += ["-c:v", "libsvtav1", "-crf", f"{plan.crf:g}", "-preset", str(plan.preset)]
        params = _svtav1_params(plan)
        if params:
            args += ["-svtav1-params", params]
        args += ["-g", str(plan.keyint_frames)]
    elif plan.encoder == "av1_qsv":
        args += ["-c:v", "av1_qsv", "-global_quality", f"{plan.crf:g}",
                 "-preset", str(_QSV_PRESET_MAP.get(plan.preset, 4)),
                 "-g", str(plan.keyint_frames)]
        if plan.low_power:
            args += ["-low_power", "1"]
        args += ["-extbrc", "1", "-look_ahead_depth", "40"]
    elif plan.encoder == "av1_vaapi":
        args += ["-c:v", "av1_vaapi", "-qp", f"{plan.crf:g}", "-g", str(plan.keyint_frames)]
    else:
        raise ValueError(f"unsupported encoder {plan.encoder}")

    # --- colour metadata must survive, especially for HDR ---
    if info.color_primaries:
        args += ["-color_primaries", info.color_primaries]
    if info.color_transfer:
        args += ["-color_trc", info.color_transfer]
    if info.color_space:
        args += ["-colorspace", info.color_space]

    if not quiet_streams:
        # --- audio ---
        out_index = 0
        for a in plan.audio:
            if a.get("action") == "drop":
                continue
            if a.get("action") == "opus":
                channels = int(a.get("channels") or 2)
                args += [f"-c:a:{out_index}", "libopus",
                         f"-b:a:{out_index}", str(int(a.get("bitrate") or 128_000))]
                if channels > 2:
                    # Required for correct multichannel Opus mapping.
                    args += [f"-mapping_family:a:{out_index}", "1"]
                args += [f"-vbr:a:{out_index}", "on", f"-application:a:{out_index}", "audio"]
            else:
                args += [f"-c:a:{out_index}", "copy"]
            out_index += 1

        # --- subtitles / metadata ---
        if any(s.get("action") != "drop" for s in plan.subtitles):
            args += ["-c:s", "copy"]
        if plan.copy_attachments and info.attachments:
            args += ["-c:t", "copy"]
        args += ["-map_metadata", "0"]
        args += ["-map_chapters", "0" if plan.copy_chapters else "-1"]
        args += ["-metadata", "OPTIMIZARR=av1"]

    args += ["-max_muxing_queue_size", "4096"]
    if plan.extra_args and not quiet_streams:
        try:
            args += shlex.split(plan.extra_args)
        except ValueError:
            pass

    if quiet_streams:
        args += ["-f", "matroska"]
    args.append(dest)
    return args


def estimate_audio_bitrate(plan: EncodePlan, info: MediaInfo) -> int:
    """Total bits/s of the audio tracks that survive the plan."""
    total = 0
    for a in plan.audio:
        if a.get("action") == "drop":
            continue
        if a.get("action") == "opus":
            total += int(a.get("bitrate") or 0)
        else:
            total += int(a.get("bitrate") or 0) or int(a.get("channels") or 2) * 96_000
    return total


def estimate_overhead_bitrate(info: MediaInfo) -> int:
    """Subtitles, chapters and container overhead - small but not zero."""
    subs = len(info.subtitle_streams) * 2_000
    container = 4_000
    return subs + container


def sample_positions(duration: float, count: int, skip_pct: float) -> list[float]:
    """Evenly spread probe positions, avoiding intro/outro."""
    if duration <= 0:
        return [0.0]
    skip = max(0.0, min(0.4, skip_pct))
    start = duration * skip
    end = duration * (1.0 - skip)
    usable = max(end - start, 1.0)
    if count <= 1:
        return [start + usable / 2]
    step = usable / count
    return [start + step * (i + 0.5) for i in range(count)]


def clamp_crf(value: float, settings: AppSettings) -> float:
    lo = float(min(settings.encoding.crf_min, settings.encoding.crf_max))
    hi = float(max(settings.encoding.crf_min, settings.encoding.crf_max))
    return max(lo, min(hi, value))


def qp_for_encoder(crf: float, encoder: str) -> float:
    """QSV/VAAPI quality scales are close enough to CRF to reuse it directly.

    VAAPI QP tends to run a touch hotter than SVT-AV1 CRF, so shave a little to
    land on comparable quality.
    """
    if encoder == "av1_vaapi":
        return max(1.0, math.floor(crf * 0.95))
    return crf
