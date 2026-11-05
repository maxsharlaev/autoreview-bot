"""Admin control UI static mount (no Node required)."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.api.admin_ui import CONTROL_MOUNT, mount_admin_ui
from app.config import Settings
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _settings(**over: object) -> Settings:
    base = {
        "admin_ui_enabled": False,
        "admin_ui_dist": "admin_ui_out",
        "admin_api_enabled": False,
    }
    base.update(over)
    return Settings(**base)


def _fixture_dist() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "admin_ui_out"


def test_control_not_mounted_when_disabled() -> None:
    app = FastAPI()
    mount_admin_ui(app, _settings(admin_ui_enabled=False, admin_ui_dist=str(_fixture_dist())))
    client = TestClient(app)
    assert client.get(CONTROL_MOUNT + "/").status_code == 404


def test_control_serves_index_when_enabled(tmp_path: Path) -> None:
    dist = tmp_path / "out"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Control</title><h1>Control fixture</h1>", encoding="utf-8")
    (dist / "login").mkdir()
    (dist / "login" / "index.html").write_text("<!doctype html><title>Sign in</title>", encoding="utf-8")

    app = FastAPI()
    mount_admin_ui(app, _settings(admin_ui_enabled=True, admin_ui_dist=str(dist)))
    client = TestClient(app)

    root = client.get(CONTROL_MOUNT + "/")
    assert root.status_code == 200
    assert b"Control fixture" in root.content

    login = client.get(CONTROL_MOUNT + "/login/")
    assert login.status_code == 200
    assert b"Sign in" in login.content


def test_control_missing_dist_fails_closed(tmp_path: Path) -> None:
    app = FastAPI()
    with pytest.raises(RuntimeError, match="ADMIN_UI_ENABLED"):
        mount_admin_ui(app, _settings(admin_ui_enabled=True, admin_ui_dist=str(tmp_path / "missing")))


def test_repo_fixture_tree_has_login_page() -> None:
    """Smoke: committed fixture mirrors the export shape used in mount tests."""
    dist = _fixture_dist()
    assert (dist / "index.html").is_file()
    assert (dist / "login" / "index.html").is_file()
