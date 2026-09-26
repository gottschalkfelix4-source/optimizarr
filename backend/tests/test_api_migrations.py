"""One-time start-up migrations: old GPU learning samples, the old trash folder."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import CSRF_HEADERS, isolated_app_state
from app import config, db, main
from app.core import worker
from app.models import HistoryEntry, LearningSample


def _samples(*encoders: str) -> None:
    with db.session_scope() as s:
        for enc in encoders:
            s.add(LearningSample(features={"crf": 30.0}, predicted_bitrate=1e6,
                                 actual_bitrate=1e6, encoder=enc, crf=30.0))


def _encoders() -> list[str]:
    with db.session_scope() as s:
        return sorted(r.encoder for r in s.query(LearningSample).all())


def _history() -> list[str]:
    with db.session_scope() as s:
        return [h.message for h in s.query(HistoryEntry).all()]


# --- 3. learning samples of the old GPU quality handling ------------------------- #

def test_old_gpu_samples_are_dropped_once(tmp_path):
    with isolated_app_state(tmp_path):
        _samples("av1_vaapi", "av1_vaapi", "av1_qsv", "libsvtav1")
        assert worker.reset_legacy_hw_samples(tmp_path / "config") == 3
        assert _encoders() == ["libsvtav1"]
        messages = _history()
        assert len(messages) == 1 and "3 Messwert(e)" in messages[0]
        assert (tmp_path / "config" / worker.HW_SAMPLES_RESET_MARKER).exists()

        # New GPU samples come from the new CRF mapping and stay.
        _samples("av1_vaapi")
        assert worker.reset_legacy_hw_samples(tmp_path / "config") == 0
        assert _encoders() == ["av1_vaapi", "libsvtav1"]
        assert len(_history()) == 1


def test_nothing_to_drop_writes_no_history_but_the_marker(tmp_path):
    with isolated_app_state(tmp_path):
        _samples("libsvtav1")
        assert worker.reset_legacy_hw_samples(tmp_path / "config") == 0
        assert _history() == []
        assert (tmp_path / "config" / worker.HW_SAMPLES_RESET_MARKER).exists()


# --- 4. the stored /config/trash ------------------------------------------------- #

@pytest.mark.parametrize("stored", ["/config/trash", "/config/trash/"])
def test_the_old_trash_default_becomes_the_library_folder(tmp_path, stored):
    with isolated_app_state(tmp_path):
        cfg = config.AppSettings()
        cfg.output.trash_dir = stored
        assert main._migrate_trash_dir(cfg) is True
        assert cfg.output.trash_dir == ""
        assert any("Papierkorb umgestellt" in m for m in _history())


def test_the_trash_migration_runs_only_once(tmp_path):
    with isolated_app_state(tmp_path):
        assert main._migrate_trash_dir(config.AppSettings()) is False
        # Chosen deliberately after the switch: left alone.
        cfg = config.AppSettings()
        cfg.output.trash_dir = "/config/trash"
        assert main._migrate_trash_dir(cfg) is False
        assert cfg.output.trash_dir == "/config/trash"
        assert _history() == []


def test_a_different_trash_folder_is_kept(tmp_path):
    with isolated_app_state(tmp_path):
        cfg = config.AppSettings()
        cfg.output.trash_dir = "/mnt/user/trash"
        assert main._migrate_trash_dir(cfg) is False
        assert cfg.output.trash_dir == "/mnt/user/trash"


# --- both, through the real start-up --------------------------------------------- #

def test_startup_runs_both_migrations_before_the_first_fit(tmp_path, monkeypatch):
    fits: list[list[str]] = []

    def refit() -> dict:
        fits.append(_encoders())
        return {}

    monkeypatch.setattr(worker, "refit_predictor", refit)
    with isolated_app_state(tmp_path):
        stored = config.load_settings(force=True)
        stored.output.trash_dir = "/config/trash"
        config.save_settings(stored)
        _samples("av1_qsv", "libsvtav1")
        with TestClient(main.app, headers=CSRF_HEADERS):
            pass
        assert fits and fits[0] == ["libsvtav1"]
        assert config.load_settings(force=True).output.trash_dir == ""
        messages = _history()
        assert any("Lernmodell bereinigt" in m for m in messages)
        assert any("Papierkorb umgestellt" in m for m in messages)


def test_cpu_threads_description_names_what_it_limits():
    text = config.QueueSettings.model_fields["cpu_threads"].description or ""
    assert "lp" not in text.split()
    assert "pin" in text and "decoder" in text
