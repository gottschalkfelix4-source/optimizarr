"""System info, hardware detection and settings endpoints."""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config as cfg
from ..config import AppSettings, PROFILE_PRESETS, apply_profile, load_settings, save_settings, update_settings
from ..core import ffmpeg, hwaccel, predictor, scanner, worker
from ..core.advisor import get_advisor, sdk_available
from ..core.events import bus
from ..version import __version__

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/system/info")
async def system_info() -> dict[str, Any]:
    settings = load_settings()
    hw = hwaccel.cached()
    model = predictor.model()
    return {
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "ffmpeg": {
            "binary": ffmpeg.FFMPEG,
            "version": await ffmpeg.version(),
            "encoders": sorted(
                e for e in await ffmpeg.available_encoders()
                if "av1" in e or e in ("libopus", "libsvtav1")
            ),
        },
        "hardware": hw.to_dict() if hw else None,
        "learning_model": model.stats(),
        "advisor": _advisor_info(settings),
        "scan": scanner.state.snapshot(),
        "queue": worker.queue_worker.status(),
        "next_scan": worker.scheduler.next_scan.isoformat() if worker.scheduler.next_scan else None,
        "paths": {
            "config": str(cfg.CONFIG_DIR),
            "transcode": str(cfg.TRANSCODE_DIR),
            "transcode_free_gb": round(_free_gb(cfg.TRANSCODE_DIR), 1),
        },
    }


def _advisor_info(settings) -> dict[str, Any]:
    """Which backend is active and whether it could actually answer right now."""
    advisor = get_advisor(settings.advisor)
    ready, reason = advisor.readiness()
    return {
        "sdk_installed": sdk_available(),
        "enabled": settings.advisor.enabled,
        "provider": settings.advisor.provider,
        "configured": ready,
        "reason": reason,
        "model": advisor.provider.describe_model(),
        "calls_used": advisor.calls_used,
    }


def _free_gb(path: Path) -> float:
    try:
        return shutil.disk_usage(str(path)).free / 1024**3
    except OSError:
        return 0.0


@router.post("/system/detect-hardware")
async def detect_hardware() -> dict[str, Any]:
    settings = load_settings()
    report = await hwaccel.detect(
        settings.hardware.render_device, settings.hardware.qsv_low_power, force=True
    )
    bus.publish("hardware.detected", {"summary": report.summary})
    return report.to_dict()


@router.get("/system/render-devices")
def render_devices() -> dict[str, Any]:
    """List the render nodes present in the container, for the settings dropdown."""
    devices: list[dict[str, Any]] = []
    dri = Path("/dev/dri")
    if dri.is_dir():
        for entry in sorted(dri.iterdir()):
            if entry.name.startswith("renderD") or entry.name.startswith("card"):
                devices.append({
                    "path": str(entry),
                    "writable": os.access(str(entry), os.R_OK | os.W_OK),
                    "is_render_node": entry.name.startswith("renderD"),
                })
    return {"devices": devices, "dri_present": dri.is_dir()}


@router.post("/system/refit-model")
async def refit_model() -> dict[str, Any]:
    stats = await asyncio.to_thread(worker.refit_predictor)
    return stats


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return load_settings(force=True).model_dump(mode="json")


@router.get("/settings/schema")
def settings_schema() -> dict[str, Any]:
    """Field metadata so the UI can render help texts and ranges from one source."""
    return {
        "schema": AppSettings.model_json_schema(),
        "profiles": PROFILE_PRESETS,
    }


class SettingsPatch(BaseModel):
    model_config = {"extra": "allow"}


@router.put("/settings")
def put_settings(patch: dict[str, Any]) -> dict[str, Any]:
    try:
        settings = update_settings(patch)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Ungueltige Einstellungen: {exc}") from exc
    bus.publish("settings.changed", {"groups": list(patch.keys())})
    return settings.model_dump(mode="json")


@router.post("/settings/profile/{name}")
def set_profile(name: str) -> dict[str, Any]:
    if name not in PROFILE_PRESETS:
        raise HTTPException(status_code=404, detail=f"Unbekanntes Profil: {name}")
    settings = apply_profile(load_settings(), name)
    saved = save_settings(settings)
    bus.publish("settings.changed", {"profile": name})
    return saved.model_dump(mode="json")


@router.post("/settings/test-advisor")
async def test_advisor(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Kept for compatibility - the provider-aware version lives in routes_advisor."""
    from .routes_advisor import TestRequest, test_provider

    return await test_provider(TestRequest(**(payload or {})))


@router.post("/settings/reset")
def reset_settings() -> dict[str, Any]:
    settings = save_settings(AppSettings())
    bus.publish("settings.changed", {"reset": True})
    return settings.model_dump(mode="json")
