"""Settings: validation of risky fields, partial updates, locking, stored data."""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, security
from app.config import AppSettings, load_settings, update_settings
from app.main import app
from app.models import Setting

from conftest import CSRF_HEADERS, isolated_app_state


@pytest.fixture()
def client(tmp_path):
    with isolated_app_state(tmp_path):
        yield TestClient(app, headers=CSRF_HEADERS)


# --------------------------------------------------------------------------- #
# extra_ffmpeg_args
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("args", [
    "",
    "-svtav1-params tune=0:enable-overlays=1",
    "-g 240 -maxrate:v 8M -bufsize:v 16M",
    "-metadata title=\"AC/DC Live\" -disposition:s:0 default",
    "-look_ahead_depth 40 -extbrc 1",
    "-qp:v -1",
])
def test_harmless_extra_args_are_accepted(args):
    assert security.check_extra_ffmpeg_args(args) == args.strip()


@pytest.mark.parametrize("args", [
    "/tmp/copy.mkv",                        # a second output file
    "-map 0 /config/leak.mkv",              # stream copy into a path
    "-y",                                   # overwrite
    "-f tee",                               # format writing elsewhere
    "-i http://evil/x",                     # another input, any protocol
    "-vf movie=/etc/passwd",                # filters read files
    "-filter_complex_script x",
    "-attach /config/optimizarr.db",
    "-passlogfile /config/x",
    "-svtav1-params stat-file=pass.stat",   # svt writes the stats file itself
    "-metadata",                            # value missing
    "-g -maxrate 1M",                       # option taken as value
    "-c:v copy",                            # bypasses the planner
    "-t 10",                                # truncates the output
    "-g 'unterminated",
])
def test_dangerous_extra_args_are_rejected(args):
    with pytest.raises(ValueError):
        security.check_extra_ffmpeg_args(args)


def test_put_with_dangerous_extra_args_is_422(client):
    r = client.put("/api/settings", json={"encoding": {"extra_ffmpeg_args": "-y /tmp/out.mkv"}})
    assert r.status_code == 422
    assert "encoding.extra_ffmpeg_args" in r.json()["detail"]
    assert load_settings().encoding.extra_ffmpeg_args == ""


# --------------------------------------------------------------------------- #
# file_mode, output_dir, trash_dir
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    ("0664", "0664"), ("664", "0664"), ("0600", "0600"), ("0777", "0777"), ("0o644", "0644"),
])
def test_valid_file_modes(value, expected):
    assert security.check_file_mode(value) == expected


@pytest.mark.parametrize("value", ["4755", "2775", "1777", "0400", "0000", "999", "rw-r--r--", "", "07777"])
def test_invalid_file_modes(value):
    with pytest.raises(ValueError):
        security.check_file_mode(value)


def test_put_rejects_setuid_file_mode(client):
    r = client.put("/api/settings", json={"output": {"file_mode": "4777"}})
    assert r.status_code == 422
    assert load_settings().output.file_mode == "0664"


@pytest.mark.parametrize("field", ["output_dir", "trash_dir"])
@pytest.mark.parametrize("value", ["relative/dir", "/", "/etc", "/usr/local/x", "/proc/1", "/lib64", "/app/static"])
def test_directories_must_be_absolute_and_outside_system_paths(client, field, value):
    r = client.put("/api/settings", json={"output": {field: value}})
    assert r.status_code == 422, value


@pytest.mark.parametrize("field", ["output_dir", "trash_dir"])
def test_normal_directories_are_accepted(client, field):
    r = client.put("/api/settings", json={"output": {field: "/media/av1-out/"}})
    assert r.status_code == 200
    assert r.json()["output"][field] == "/media/av1-out"
    assert client.put("/api/settings", json={"output": {field: ""}}).status_code == 200


def test_library_like_names_are_not_system_paths():
    assert not security.is_system_path("/library/movies")
    assert not security.is_system_path("/media/lib")
    assert security.is_system_path("/lib")
    assert security.is_system_path("/libx32/foo")


# --------------------------------------------------------------------------- #
# Partial updates and locking
# --------------------------------------------------------------------------- #

def test_put_changes_only_the_fields_sent(client):
    update_settings({"queue": {"paused": True, "max_concurrent_jobs": 2}})
    r = client.put("/api/settings", json={"queue": {"max_concurrent_jobs": 3}})
    assert r.status_code == 200
    queue = load_settings().queue
    assert queue.paused is True and queue.max_concurrent_jobs == 3


def test_deep_merge_merges_nested_dicts():
    base = {"a": {"x": 1, "inner": {"k": 1, "l": 2}}, "b": 2}
    config._deep_merge(base, {"a": {"inner": {"l": 3}}})
    assert base == {"a": {"x": 1, "inner": {"k": 1, "l": 3}}, "b": 2}


def test_a_group_must_be_an_object(client):
    r = client.put("/api/settings", json={"queue": None})
    assert r.status_code == 422
    assert load_settings().queue.max_concurrent_jobs == 1


def test_concurrent_updates_do_not_lose_each_other(client, monkeypatch):
    """Two saves of different groups at the same time must both survive."""
    original = config.save_settings

    def slow_save(settings):
        time.sleep(0.2)  # widen the read-modify-write window
        return original(settings)

    monkeypatch.setattr(config, "save_settings", slow_save)
    errors: list[BaseException] = []

    def run(patch):
        try:
            update_settings(patch)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=({"encoding": {"crf": 22}},)),
        threading.Thread(target=run, args=({"queue": {"paused": True}},)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    settings = config.load_settings(force=True)
    assert settings.encoding.crf == 22
    assert settings.queue.paused is True


# --------------------------------------------------------------------------- #
# Stored data
# --------------------------------------------------------------------------- #

def test_ui_theme_and_language_are_gone():
    ui = AppSettings().ui.model_dump()
    assert "theme" not in ui and "language" not in ui
    assert set(ui) == {"size_unit", "dashboard_refresh_seconds"}


def test_one_bad_stored_value_does_not_reset_its_group(client):
    with db.session_scope() as s:
        s.merge(Setting(key="encoding", value={
            "crf": 26, "preset": 4, "extra_ffmpeg_args": "-y /tmp/x.mkv",
        }))
        s.merge(Setting(key="ui", value={"theme": "light", "size_unit": "decimal"}))
    settings = config.load_settings(force=True)
    assert settings.encoding.crf == 26 and settings.encoding.preset == 4
    assert settings.encoding.extra_ffmpeg_args == ""   # dropped, not executed
    assert settings.ui.size_unit == "decimal"          # old "theme" is ignored


def test_settings_patch_model_is_gone():
    from app.api import routes_system

    assert not hasattr(routes_system, "SettingsPatch")
