"""Worker startup and cron registration with valid and invalid owner configs.

The owner registry is built for real from a temporary config.yaml and process env
(get_settings/get_app_config/get_owner_registry caches cleared around each test).
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
import yaml
from app.config import get_app_config, get_settings
from app.owners.registry import OwnerConfigError, get_owner_registry
from app.workers import settings as worker_settings

LEGACY_ENV = ("GITHUB_TOKEN", "GITHUB_PAT", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "OWNER_ORG_A_GITHUB_TOKEN")

# org-a uses a PAT: valid with OWNER_ORG_A_GITHUB_TOKEN set, a real startup error without it.
ORG_A = {"owners": {"org-a": {"github": {"auth": "pat", "allowed_repos": ["org-a/*"]}}}}
EMPTY: dict = {}


def _clear_caches() -> None:
    get_settings.cache_clear()
    get_app_config.cache_clear()
    get_owner_registry.cache_clear()


@pytest.fixture
def owner_config(monkeypatch, tmp_path):
    def apply(data: dict, env: dict[str, str] | None = None) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        monkeypatch.chdir(tmp_path)  # no .env from the working tree
        monkeypatch.setenv("CONFIG_PATH", str(path))
        for name in LEGACY_ENV:
            monkeypatch.delenv(name, raising=False)
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value)
        _clear_caches()

    yield apply
    _clear_caches()


async def _start(caplog) -> list[str]:
    with (
        patch.object(worker_settings, "configure_logging"),
        patch.object(worker_settings, "start_worker_metrics_server") as metrics,
        patch.object(worker_settings, "create_engine") as engine,
        patch.object(worker_settings, "create_session_factory"),
        caplog.at_level(logging.INFO, logger=worker_settings.logger.name),
    ):
        try:
            await worker_settings.startup({})
        finally:
            started = metrics.called or engine.called
    messages = [r.getMessage() for r in caplog.records]
    return messages + (["<resources started>"] if started else [])


@pytest.mark.asyncio
async def test_worker_startup_aborts_on_invalid_owner_config(owner_config, caplog) -> None:
    owner_config(ORG_A)

    with pytest.raises(SystemExit) as exc:
        await _start(caplog)

    assert exc.value.code == 1
    assert isinstance(exc.value.__cause__, OwnerConfigError)
    messages = [r.getMessage() for r in caplog.records]
    assert any("Owner configuration error" in m and "OWNER_ORG_A_GITHUB_TOKEN" in m for m in messages)
    assert not any(m.startswith("worker ready") for m in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "env"),
    [
        (ORG_A, {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_a"}),  # mode C
        (EMPTY, {"GITHUB_TOKEN": "ghp_legacy"}),  # mode A
        (EMPTY, {}),  # mode A without credentials: tolerated like the API
    ],
    ids=["mode-c", "mode-a", "mode-a-no-credentials"],
)
async def test_worker_startup_with_valid_config_reports_ready(owner_config, caplog, data, env) -> None:
    owner_config(data, env)

    messages = await _start(caplog)

    assert any(m.startswith("worker ready") for m in messages)
    assert "<resources started>" in messages


def test_digest_check_does_not_hide_invalid_owner_config(owner_config) -> None:
    owner_config(ORG_A)
    config = get_app_config()
    assert config.features.digest_enabled is True

    with pytest.raises(OwnerConfigError, match="OWNER_ORG_A_GITHUB_TOKEN"):
        worker_settings._digest_enabled_for_any_owner(config)
    with pytest.raises(OwnerConfigError):
        worker_settings._cron_jobs()


@pytest.mark.parametrize(
    ("data", "env", "jobs"),
    [
        (ORG_A, {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_a"}, 1),
        ({**ORG_A, "features": {"digest_enabled": False}}, {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_a"}, 0),
        (EMPTY, {}, 1),
    ],
    ids=["owner-digest-on", "owner-digest-off", "mode-a-no-credentials"],
)
def test_cron_registration_with_valid_config(owner_config, data, env, jobs) -> None:
    owner_config(data, env)
    assert len(worker_settings._cron_jobs()) == jobs
