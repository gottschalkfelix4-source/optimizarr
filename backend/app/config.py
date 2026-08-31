"""Application settings.

Everything is configured through the web UI and stored in SQLite - there are no
environment variables for behaviour.  The only env vars used at all are the two
paths the container needs before a database exists (config dir, transcode dir).

The Pydantic models below are the single source of truth: they define the
defaults, the validation rules and, via ``model_json_schema()``, the contract the
frontend renders against.  Each top-level group is persisted as one row in the
``settings`` table, so adding a field later just falls back to its default.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

CONFIG_DIR = Path(os.environ.get("OPTIMIZARR_CONFIG_DIR", "/config"))
TRANSCODE_DIR = Path(os.environ.get("OPTIMIZARR_TRANSCODE_DIR", "/transcode"))
DEFAULT_MEDIA_ROOT = os.environ.get("OPTIMIZARR_MEDIA_ROOT", "/media")

VIDEO_EXTENSIONS_DEFAULT = [
    "mkv", "mp4", "m4v", "avi", "mov", "wmv", "ts", "m2ts", "mts",
    "mpg", "mpeg", "vob", "flv", "webm", "divx", "ogm", "rmvb", "asf",
]


class LibrarySettings(BaseModel):
    """What gets scanned."""

    extensions: list[str] = Field(default_factory=lambda: list(VIDEO_EXTENSIONS_DEFAULT))
    min_file_size_mb: int = Field(50, ge=0, description="Ignore files smaller than this")
    min_duration_seconds: int = Field(60, ge=0, description="Ignore clips shorter than this")
    exclude_patterns: list[str] = Field(
        default_factory=lambda: ["*/.recycle/*", "*/@eaDir/*", "*sample*", "*/extras/*", "*/featurettes/*"]
    )
    follow_symlinks: bool = False
    scan_on_start: bool = True
    scan_interval_hours: int = Field(24, ge=0, le=720, description="0 disables the periodic scan")
    rescan_changed_only: bool = True
    reanalyze_after_days: int = Field(90, ge=0, description="Re-run analysis on stale results")


class AnalysisSettings(BaseModel):
    """How hard Optimizarr thinks before proposing a conversion."""

    mode: Literal["quick", "sample", "vmaf"] = Field(
        "sample",
        description=(
            "quick = metadata heuristics only (seconds per file); "
            "sample = short trial encodes for a real size measurement; "
            "vmaf = trial encodes plus VMAF quality search for the best CRF"
        ),
    )
    sample_count: int = Field(3, ge=1, le=10, description="Number of probe segments per file")
    sample_duration: int = Field(12, ge=4, le=60, description="Seconds per probe segment")
    sample_skip_intro_pct: float = Field(0.05, ge=0.0, le=0.4)
    target_vmaf: float = Field(94.0, ge=70.0, le=100.0, description="Quality target for the CRF search")
    vmaf_search_steps: int = Field(4, ge=1, le=8)
    min_saving_percent: float = Field(
        20.0, ge=1.0, le=90.0, description="Below this predicted saving a file is skipped"
    )
    min_saving_mb: int = Field(100, ge=0, description="Absolute floor - tiny wins are not worth it")
    skip_codecs: list[str] = Field(default_factory=lambda: ["av1"])
    skip_if_bitrate_below_kbps: int = Field(
        0, ge=0, description="0 = auto (derived from resolution); already-lean files are skipped"
    )
    analysis_workers: int = Field(2, ge=1, le=16)
    use_learning_model: bool = True
    trust_learning_after_samples: int = Field(15, ge=3, le=500)


class EncodingSettings(BaseModel):
    """The AV1 encode itself."""

    profile: Literal["archive", "balanced", "space"] = Field(
        "balanced",
        description="archive = near-transparent, balanced = default, space = maximum shrink",
    )
    encoder: Literal["auto", "svt_av1", "av1_qsv", "av1_vaapi"] = Field(
        "auto", description="auto picks hardware AV1 if the GPU supports it, else SVT-AV1 on CPU"
    )
    preset: int = Field(6, ge=0, le=13, description="SVT-AV1 preset: lower = slower and smaller")
    crf: int = Field(30, ge=1, le=63, description="Base quality. The analyzer adjusts per file.")
    allow_crf_adjust: bool = Field(True, description="Let the analyzer move CRF to hit the VMAF target")
    crf_min: int = Field(20, ge=1, le=63)
    crf_max: int = Field(45, ge=1, le=63)
    force_10bit: bool = Field(True, description="10-bit AV1 compresses better even for 8-bit sources")
    film_grain_synthesis: int = Field(
        0, ge=0, le=50, description="0 = off / auto-detect per file, otherwise a fixed denoise level"
    )
    auto_film_grain: bool = True
    max_width: int = Field(0, ge=0, description="0 = keep source resolution, else downscale cap")
    keyframe_interval_seconds: int = Field(5, ge=1, le=30)
    deinterlace: bool = True
    copy_chapters: bool = True
    copy_attachments: bool = True
    container: Literal["mkv", "mp4"] = "mkv"
    extra_ffmpeg_args: str = Field("", description="Appended verbatim - power users only")
    max_encode_hours: int = Field(12, ge=1, le=72, description="Abort an encode that runs this long")


class AudioSettings(BaseModel):
    mode: Literal["copy", "opus", "opus_if_bloated"] = Field(
        "opus_if_bloated",
        description="opus_if_bloated re-encodes only tracks above the bitrate threshold",
    )
    opus_bitrate_per_channel: int = Field(48, ge=24, le=128)
    bloat_threshold_kbps_per_channel: int = Field(96, ge=32, le=512)
    keep_languages: list[str] = Field(
        default_factory=list, description="Empty = keep all. Example: deu, eng"
    )
    drop_commentary: bool = False
    keep_default_track_always: bool = True


class SubtitleSettings(BaseModel):
    mode: Literal["copy", "drop", "text_only"] = "copy"
    keep_languages: list[str] = Field(default_factory=list)


class OutputSettings(BaseModel):
    """What happens to the file when the encode finishes."""

    mode: Literal["replace", "sidecar", "separate_dir"] = Field(
        "replace", description="replace swaps the original, sidecar writes next to it"
    )
    output_dir: str = ""
    sidecar_suffix: str = ".av1"
    original_action: Literal["delete", "trash", "keep"] = Field(
        "trash", description="trash moves the source into the recycle folder below"
    )
    trash_dir: str = "/config/trash"
    trash_retention_days: int = Field(14, ge=0, le=365, description="0 = keep forever")
    preserve_mtime: bool = True
    set_permissions: bool = True
    file_mode: str = "0664"
    uid: int = Field(99, ge=0)
    gid: int = Field(100, ge=0)
    # --- safety gates: nothing replaces an original unless all of these pass ---
    require_smaller: bool = True
    min_accept_saving_percent: float = Field(
        5.0, ge=0.0, le=90.0, description="Reject the result if it saved less than this"
    )
    verify_output: bool = Field(True, description="Re-probe the result and compare duration/streams")
    max_duration_drift_seconds: float = Field(2.0, ge=0.1, le=60.0)
    verify_vmaf: bool = Field(False, description="Measure VMAF on the finished file before accepting")
    min_accept_vmaf: float = Field(90.0, ge=50.0, le=100.0)


class QueueSettings(BaseModel):
    max_concurrent_jobs: int = Field(1, ge=1, le=8)
    auto_queue_candidates: bool = Field(
        False, description="Queue every new candidate automatically instead of asking"
    )
    auto_queue_min_saving_percent: float = Field(25.0, ge=1.0, le=90.0)
    paused: bool = False
    schedule_enabled: bool = False
    schedule_start: str = Field("22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    schedule_end: str = Field("07:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    schedule_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    cpu_threads: int = Field(0, ge=0, description="0 = all cores (SVT-AV1 lp parameter)")
    nice_level: int = Field(10, ge=-20, le=19)
    min_free_disk_gb: int = Field(20, ge=0, description="Refuse to start a job below this")


class HardwareSettings(BaseModel):
    render_device: str = Field("/dev/dri/renderD128", description="Intel render node")
    hw_decode: bool = Field(True, description="Decode on the iGPU/Arc to free up the CPU")
    hw_encode: bool = Field(True, description="Use the GPU AV1 encoder when it exists")
    qsv_low_power: bool = Field(True, description="VDENC path - required on most Intel parts")
    fallback_to_cpu: bool = Field(True, description="Retry on SVT-AV1 if a hardware encode fails")
    detect_on_start: bool = True


class AdvisorSettings(BaseModel):
    """Optional AI layer that reviews the local decision.

    Three backends are supported and only one is active at a time.  The local
    analyzer works entirely without any of them; the advisor can only refine a
    decision that has already been made.
    """

    enabled: bool = False
    provider: Literal["anthropic", "openai_compatible", "openai_codex"] = Field(
        "anthropic",
        description=(
            "anthropic = Claude API key; "
            "openai_compatible = any OpenAI-style endpoint (URL + model + key); "
            "openai_codex = sign in with a ChatGPT account via the browser"
        ),
    )

    # --- Anthropic ---
    api_key: str = ""
    model: str = "claude-opus-5"

    # --- any OpenAI-compatible endpoint (OpenAI, OpenRouter, Ollama, LM Studio, ...) ---
    openai_base_url: str = Field(
        "", description="Base URL, e.g. https://api.openai.com/v1 or http://192.168.1.5:11434/v1"
    )
    openai_api_key: str = ""
    openai_model: str = Field("", description="Model name exactly as the endpoint expects it")
    openai_structured_mode: Literal["auto", "json_schema", "json_object", "prompt"] = Field(
        "auto",
        description=(
            "How to force JSON. auto probes what the endpoint accepts and remembers it; "
            "prompt works everywhere but is the least reliable"
        ),
    )
    openai_max_tokens: int = Field(4000, ge=256, le=32000)
    openai_temperature: float = Field(0.2, ge=0.0, le=2.0)
    openai_send_system_role: bool = Field(
        True, description="Some endpoints reject a system message - turn this off if so"
    )

    # --- ChatGPT sign-in (Codex).  Tokens live in the oauth_credentials table. ---
    codex_model: str = Field(
        "gpt-5.6-sol",
        description=(
            "Model requested over the ChatGPT backend. Slugs rotate and depend on the "
            "plan - the settings screen can fetch the account's actual list."
        ),
    )
    codex_reasoning_effort: Literal["low", "medium", "high"] = "low"

    # --- shared behaviour ---
    mode: Literal["uncertain_only", "all_candidates", "explain_only"] = Field(
        "uncertain_only",
        description=(
            "uncertain_only asks when the local model is unsure; "
            "all_candidates asks for every file; explain_only never changes settings"
        ),
    )
    allow_setting_changes: bool = Field(True, description="Let the advisor nudge CRF/grain")
    max_crf_delta: int = Field(4, ge=0, le=15, description="Clamp on how far the advisor may move CRF")
    max_calls_per_scan: int = Field(50, ge=0, le=5000)
    uncertain_below_confidence: float = Field(0.6, ge=0.0, le=1.0)
    timeout_seconds: int = Field(45, ge=5, le=300)
    include_filename: bool = Field(
        True, description="Filenames help spot anime, grainy classics, cam rips"
    )

    @field_validator("openai_base_url")
    @classmethod
    def _clean_base_url(cls, v: str) -> str:
        return v.strip().rstrip("/")


class NotificationSettings(BaseModel):
    webhook_url: str = ""
    notify_on_job_done: bool = False
    notify_on_job_failed: bool = True
    notify_on_scan_done: bool = False


class UiSettings(BaseModel):
    theme: Literal["dark", "light", "system"] = "dark"
    language: Literal["de", "en"] = "de"
    size_unit: Literal["binary", "decimal"] = "binary"
    dashboard_refresh_seconds: int = Field(3, ge=1, le=60)


class AppSettings(BaseModel):
    """The whole configuration tree."""

    library: LibrarySettings = Field(default_factory=LibrarySettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    encoding: EncodingSettings = Field(default_factory=EncodingSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    subtitles: SubtitleSettings = Field(default_factory=SubtitleSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    hardware: HardwareSettings = Field(default_factory=HardwareSettings)
    advisor: AdvisorSettings = Field(default_factory=AdvisorSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    ui: UiSettings = Field(default_factory=UiSettings)

    @field_validator("library")
    @classmethod
    def _normalise_extensions(cls, v: LibrarySettings) -> LibrarySettings:
        v.extensions = [e.lower().lstrip(".") for e in v.extensions if e.strip()]
        return v


# --------------------------------------------------------------------------- #
# Quality profiles - opinionated presets the UI exposes as one-click choices.
# They seed CRF/preset; per-file analysis still adjusts within crf_min..crf_max.
# --------------------------------------------------------------------------- #
PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "archive": {
        "crf": 24, "preset": 4, "target_vmaf": 96.0,
        "min_saving_percent": 15.0, "label": "Archiv",
        "hint": "Praktisch verlustfrei sichtbar. Kleinere Ersparnis, langsamster Encode.",
    },
    "balanced": {
        "crf": 30, "preset": 6, "target_vmaf": 94.0,
        "min_saving_percent": 20.0, "label": "Ausgewogen",
        "hint": "Empfohlen. Deutliche Ersparnis bei kaum sichtbarem Unterschied.",
    },
    "space": {
        "crf": 35, "preset": 8, "target_vmaf": 91.0,
        "min_saving_percent": 30.0, "label": "Platz sparen",
        "hint": "Maximale Ersparnis, schneller Encode. Auf grossen TVs sichtbar weicher.",
    },
}


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
_lock = threading.RLock()
_cache: AppSettings | None = None


def _rows_to_settings(rows: dict[str, Any]) -> AppSettings:
    """Build AppSettings from stored rows, tolerating missing/renamed fields."""
    payload: dict[str, Any] = {}
    for name in AppSettings.model_fields:
        value = rows.get(name)
        if isinstance(value, dict):
            payload[name] = value
    try:
        return AppSettings.model_validate(payload)
    except Exception:
        # A corrupt group must never take the app down - fall back per group.
        safe: dict[str, Any] = {}
        for name, field in AppSettings.model_fields.items():
            value = rows.get(name)
            if not isinstance(value, dict):
                continue
            try:
                safe[name] = field.annotation.model_validate(value)  # type: ignore[union-attr]
            except Exception:
                continue
        return AppSettings.model_validate(safe)


def load_settings(force: bool = False) -> AppSettings:
    """Read settings from the DB (cached)."""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        from .db import session_scope
        from .models import Setting

        rows: dict[str, Any] = {}
        try:
            with session_scope() as s:
                for row in s.query(Setting).all():
                    rows[row.key] = row.value
        except Exception:
            rows = {}
        _cache = _rows_to_settings(rows)
        return _cache


def save_settings(settings: AppSettings) -> AppSettings:
    """Persist the full tree, one row per group."""
    global _cache
    from .db import session_scope
    from .models import Setting

    with _lock:
        data = settings.model_dump(mode="json")
        with session_scope() as s:
            for key, value in data.items():
                row = s.get(Setting, key)
                if row is None:
                    s.add(Setting(key=key, value=value))
                else:
                    row.value = value
        _cache = settings
        return _cache


def update_settings(patch: dict[str, Any]) -> AppSettings:
    """Merge a partial update (group -> fields) into the stored settings."""
    current = load_settings().model_dump(mode="json")
    for group, values in patch.items():
        if group not in current:
            continue
        if isinstance(values, dict):
            current[group].update(values)
        else:
            current[group] = values
    return save_settings(AppSettings.model_validate(current))


def apply_profile(settings: AppSettings, profile: str | None = None) -> AppSettings:
    """Copy a quality profile onto the encoding/analysis groups."""
    name = profile or settings.encoding.profile
    preset = PROFILE_PRESETS.get(name)
    if not preset:
        return settings
    settings.encoding.profile = name  # type: ignore[assignment]
    settings.encoding.crf = int(preset["crf"])
    settings.encoding.preset = int(preset["preset"])
    settings.analysis.target_vmaf = float(preset["target_vmaf"])
    settings.analysis.min_saving_percent = float(preset["min_saving_percent"])
    return settings


def invalidate_cache() -> None:
    global _cache
    with _lock:
        _cache = None
