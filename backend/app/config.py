"""Application settings.

Behaviour is configured through the web UI and stored in SQLite, not through
environment variables.  The environment only supplies what has to be known
before a database exists, plus a few container and escape-hatch switches:

* ``PUID``, ``PGID``, ``UMASK`` - user, group and umask the entrypoint drops to
* ``TZ`` - time zone for the schedule and the logs
* ``OPTIMIZARR_CONFIG_DIR`` (``/config``) - database and settings
* ``OPTIMIZARR_TRANSCODE_DIR`` (``/transcode``) - scratch space for encodes
* ``OPTIMIZARR_MEDIA_ROOT`` (``/media``) - where the folder picker starts
* ``OPTIMIZARR_STATIC_DIR`` (``/app/static``) - the built web UI
* ``OPTIMIZARR_FFMPEG`` / ``OPTIMIZARR_FFPROBE`` - override the ffmpeg binaries
* ``OPTIMIZARR_RESET_AUTH=1`` - switch the login off at startup (forgotten password)
* ``CODEX_*`` - overrides for the ChatGPT sign-in endpoints (development only)

The Pydantic models below are the single source of truth: they define the
defaults, the validation rules and, via ``model_json_schema()``, the contract the
frontend renders against.  Each top-level group is persisted as one row in the
``settings`` table, so adding a field later just falls back to its default.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError, ValidationInfo, field_validator, model_validator

from . import security
from .core.codecs import is_excluded, normalise, normalise_list as _normalise_codecs

log = logging.getLogger(__name__)

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

    convert_all_h264: bool = Field(
        False,
        description="Convert H.264 to AV1 even without savings; larger outputs are accepted",
    )
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
    skip_codecs: list[str] = Field(
        default_factory=lambda: ["av1"],
        description="Sources in these codecs never become candidates",
    )
    skip_if_bitrate_below_kbps: int = Field(
        0, ge=0, description="0 = auto (derived from resolution); already-lean files are skipped"
    )
    analysis_workers: int = Field(2, ge=1, le=16)
    dolby_vision: Literal["skip", "hdr10_fallback"] = Field(
        "skip",
        description=(
            "skip = Dolby-Vision-Dateien nie konvertieren; hdr10_fallback = Profil 7/8 "
            "als HDR10 kodieren (die DV-Ebene geht verloren). Profil 5 hat keine "
            "HDR10-Basis und wird immer uebersprungen."
        ),
    )
    use_learning_model: bool = True
    trust_learning_after_samples: int = Field(15, ge=3, le=500)

    def requires_h264_conversion(self, codec: str) -> bool:
        return (
            self.convert_all_h264
            and normalise(codec) == "h264"
            and not is_excluded(codec, self.skip_codecs)
        )

    @field_validator("skip_codecs")
    @classmethod
    def _normalise_skip_codecs(cls, v: list[str]) -> list[str]:
        """"h265", "x265" and "HEVC" all have to mean the same thing here."""
        return _normalise_codecs(v)


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
    extra_ffmpeg_args: str = Field(
        "",
        description=(
            "Appended to the output options - power users only. Only encoder and "
            "muxer options are accepted (see security.check_extra_ffmpeg_args)"
        ),
    )
    max_encode_hours: int = Field(12, ge=1, le=72, description="Abort an encode that runs this long")

    @field_validator("extra_ffmpeg_args")
    @classmethod
    def _check_extra_args(cls, v: str) -> str:
        return security.check_extra_ffmpeg_args(v)

    @model_validator(mode="after")
    def _order_crf_range(self):
        self.crf_min, self.crf_max = sorted((self.crf_min, self.crf_max))
        return self


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
    trash_dir: str = Field(
        "",
        description=(
            "Leer = Ordner .optimizarr-trash im jeweiligen Bibliotheksordner (gleiches "
            "Dateisystem, nur Umbenennen statt Kopieren)"
        ),
    )
    trash_retention_days: int = Field(14, ge=0, le=365, description="0 = keep forever")
    preserve_mtime: bool = Field(
        False,
        description=(
            "Aenderungsdatum des Originals uebernehmen. Aus = Plex/Jellyfin erkennen die "
            "neue Datei sicher und lesen die Stream-Infos neu ein"
        ),
    )
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
    verify_full_decode: bool = False
    max_duration_drift_seconds: float = Field(2.0, ge=0.1, le=60.0)
    verify_vmaf: bool = Field(False, description="Measure VMAF on the finished file before accepting")
    min_accept_vmaf: float = Field(90.0, ge=50.0, le=100.0)
    min_quality_samples: int = Field(2, ge=1, le=2)

    @field_validator("sidecar_suffix")
    @classmethod
    def _check_suffix(cls, value: str) -> str:
        value = value.strip()
        if not value or any(c in value for c in "/\\\0\r\n"):
            raise ValueError("Namenszusatz darf nicht leer sein oder Pfadtrenner enthalten.")
        return value

    @model_validator(mode="after")
    def _require_output_directory(self):
        if self.mode == "separate_dir" and not self.output_dir:
            raise ValueError("Im Modus separater Ausgabeordner muss ein Ordner angegeben werden.")
        return self

    @field_validator("output_dir")
    @classmethod
    def _check_output_dir(cls, v: str) -> str:
        return security.check_directory_setting(v, "Ausgabeordner")

    @field_validator("trash_dir")
    @classmethod
    def _check_trash_dir(cls, v: str) -> str:
        return security.check_directory_setting(v, "Papierkorb-Ordner")

    @field_validator("file_mode")
    @classmethod
    def _check_file_mode(cls, v: str) -> str:
        return security.check_file_mode(v)


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
    schedule_days: list[Annotated[int, Field(ge=0, le=6)]] = Field(
        default_factory=lambda: [0, 1, 2, 3, 4, 5, 6], min_length=1, max_length=7,
    )

    @field_validator("schedule_days", mode="before")
    @classmethod
    def _unique_days(cls, value):
        if isinstance(value, list) and all(type(day) is int for day in value):
            return sorted(set(value))
        return value
    cpu_threads: int = Field(
        0, ge=0,
        description="0 = all cores; limits SVT-AV1 (pin) and the decoder threads per job",
    )
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
        "gpt-6-astra",
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


# Secrets never leave the server in clear text: GET /api/settings replaces a set
# value with SECRET_MASK, and a PUT carrying SECRET_MASK keeps the stored value.
SECRET_MASK = "********"
SECRET_FIELDS: tuple[tuple[str, str], ...] = (
    ("advisor", "api_key"),
    ("advisor", "openai_api_key"),
    ("notifications", "webhook_url"),
    ("security", "password"),
)


class SecuritySettings(BaseModel):
    """Optional HTTP Basic auth for the web UI and the API (off by default)."""

    auth_enabled: bool = Field(False, description="Benutzername und Passwort verlangen")
    username: str = Field("admin", min_length=1, max_length=64)
    password: str = Field(
        "",
        description="Wird nur als Hash gespeichert (pbkdf2_sha256$...)",
        validate_default=True,
    )

    @field_validator("password")
    @classmethod
    def _hash_password(cls, v: str, info: ValidationInfo) -> str:
        """Plain text never reaches the database; a stored hash is kept as is."""
        if v and not security.is_password_hash(v):
            v = security.hash_password(v)
        if info.data.get("auth_enabled") and not v:
            raise ValueError(
                "Ohne Passwort kann die Anmeldung nicht aktiviert werden. "
                "Bitte ein Passwort vergeben."
            )
        return v


class UiSettings(BaseModel):
    size_unit: Literal["binary", "decimal"] = "binary"
    dashboard_refresh_seconds: int = Field(3, ge=1, le=60)


class MaintenanceSettings(BaseModel):
    history_retention_days: int = Field(90, ge=0, le=3650)
    job_retention_days: int = Field(365, ge=0, le=3650)
    scan_retention_days: int = Field(90, ge=0, le=3650)
    restored_manifest_retention_days: int = Field(180, ge=0, le=3650)
    max_learning_samples: int = Field(10000, ge=2000, le=100000)
    max_backups: int = Field(7, ge=1, le=100)


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
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    maintenance: MaintenanceSettings = Field(default_factory=MaintenanceSettings)

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


def _validate_group(name: str, model: type[BaseModel], value: dict[str, Any]) -> BaseModel:
    """Validate one stored group, dropping only the fields that do not pass.

    A single bad value (an old enum member, a value a newer version rejects)
    must not reset the whole group to its defaults.
    """
    data = dict(value)
    while True:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            if name == "security":
                raise ValueError("Gespeicherte Anmeldung ist ungueltig.") from None
            bad = {e["loc"][0] for e in exc.errors() if e.get("loc")} & set(data)
            if not bad:
                if name == "security":
                    raise ValueError("Gespeicherte Anmeldung ist ungueltig.") from exc
                if name == "output" and data.get("mode") == "separate_dir" and not data.get("output_dir"):
                    data["mode"] = "sidecar"
                    log.warning("invalid output folder: keeping originals through sidecar output")
                    continue
                log.warning("settings group %s is unusable, using defaults", name)
                return model()
            log.warning("ignoring invalid stored setting(s) %s.%s", name, ", ".join(sorted(map(str, bad))))
            for key in bad:
                data.pop(key, None)


def _rows_to_settings(rows: dict[str, Any]) -> AppSettings:
    """Build AppSettings from stored rows, tolerating missing/renamed fields."""
    payload: dict[str, Any] = {}
    for name, field in AppSettings.model_fields.items():
        value = rows.get(name)
        if name == "security" and name in rows and not isinstance(value, dict):
            raise ValueError("Gespeicherte Anmeldung ist ungueltig.")
        if isinstance(value, dict):
            payload[name] = _validate_group(name, field.annotation, value)  # type: ignore[arg-type]
    return AppSettings.model_validate(payload)


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
            if "security" not in rows and _cache is not None and _cache.security.auth_enabled:
                rows["security"] = _cache.security.model_dump()
            settings = _rows_to_settings(rows)
        except Exception:
            log.exception("could not read settings; preserving the last valid configuration")
            if _cache is not None:
                return _cache
            raise
        _cache = settings
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


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Only what the patch names changes; nested dicts merge instead of replacing."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _drop_masked_secrets(patch: dict[str, Any]) -> dict[str, Any]:
    """A secret sent back as SECRET_MASK (or null) means "unchanged"."""
    for group, field in SECRET_FIELDS:
        values = patch.get(group)
        if isinstance(values, dict) and field in values:
            if values[field] is None or values[field] == SECRET_MASK:
                values.pop(field)
    return patch


def update_settings(patch: dict[str, Any]) -> AppSettings:
    """Merge a partial update (group -> fields) into the stored settings.

    Read, merge and write happen under one lock, so two concurrent updates of
    different fields cannot undo each other.  Unknown groups are ignored.
    """
    patch = copy.deepcopy(patch)
    with _lock:
        current = load_settings().model_dump(mode="json")
        known = {k: v for k, v in _drop_masked_secrets(patch).items() if k in current}
        for group, values in known.items():
            if not isinstance(values, dict):
                raise ValueError(f"{group}: erwartet ein Objekt mit Feldern")
        _deep_merge(current, known)
        return save_settings(AppSettings.model_validate(current))


def public_settings(settings: AppSettings) -> dict[str, Any]:
    """The settings as the API hands them out: secrets replaced by SECRET_MASK."""
    data = settings.model_dump(mode="json")
    for group, field in SECRET_FIELDS:
        values = data.get(group)
        if isinstance(values, dict) and field in values:
            values[field] = SECRET_MASK if values[field] else ""
    return data


def describe_validation_error(exc: Exception) -> str:
    """A readable German summary of a settings validation error."""
    if not isinstance(exc, ValidationError):
        return str(exc)
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


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
