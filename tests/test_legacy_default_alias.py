"""Which owner (if any) owns rows stored with the legacy owner_id 'default'."""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import AppConfig
from app.models import effective_owner_id, extract_comment_author_logins_for_owner
from app.owners.registry import OwnerConfigError, OwnerRegistry
from tests.test_multi_owner_m2_bugs import _mock_settings

LEGACY_AUTHORS = ["old-bot", {"login": "older-bot", "owner_id": "default", "kind": None}]


def _mode_a() -> OwnerRegistry:
    return OwnerRegistry.build(_mock_settings(github_token="ghp_legacy"), AppConfig(), env={})


def _mode_b() -> OwnerRegistry:
    config = AppConfig(owners={"org-b": {"default": True, "github": {"auth": "pat"}}})
    return OwnerRegistry.build(
        _mock_settings(github_token="ghp_legacy"), config, env={"OWNER_ORG_B_GITHUB_TOKEN": "ghp_b"}
    )


def _mode_c(org_a: dict | None = None, org_b: dict | None = None) -> OwnerRegistry:
    """Owners block only. Defaults: org-a has default: true, nobody has the 'default' alias."""
    config = AppConfig(
        owners={
            "org-a": {"github": {"auth": "pat"}, **({"default": True} if org_a is None else org_a)},
            "org-b": {"github": {"auth": "pat"}, **(org_b or {})},
        }
    )
    env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_a", "OWNER_ORG_B_GITHUB_TOKEN": "ghp_b"}
    return OwnerRegistry.build(_mock_settings(), config, env=env)


def _mode_c_alias_on_a_default_on_b() -> OwnerRegistry:
    return _mode_c(org_a={"aliases": ["default"]}, org_b={"default": True})


def _mode_c_alias_and_default_on_a() -> OwnerRegistry:
    return _mode_c(org_a={"aliases": ["default"], "default": True})


def test_mode_a_legacy_rows_belong_to_default() -> None:
    registry = _mode_a()
    assert registry.default_owner_id == "default"
    assert registry.legacy_default_alias() == "default"


def test_mode_b_legacy_rows_stay_with_default_not_default_flag_owner() -> None:
    registry = _mode_b()
    assert registry.default_owner_id == "org-b"
    assert registry.legacy_default_alias() == "default"


def test_mode_c_default_flag_without_alias_binds_nobody() -> None:
    registry = _mode_c()
    assert registry.default_owner_id == "org-a"
    assert registry.legacy_default_alias() is None


def test_mode_c_explicit_alias_wins_over_default_flag() -> None:
    registry = _mode_c_alias_on_a_default_on_b()
    assert registry.default_owner_id == "org-b"
    assert registry.legacy_default_alias() == "org-a"


def test_moving_default_flag_does_not_move_legacy_rows() -> None:
    assert _mode_c_alias_and_default_on_a().legacy_default_alias() == "org-a"
    assert _mode_c_alias_on_a_default_on_b().legacy_default_alias() == "org-a"


def test_alias_on_disabled_owner_binds_nobody() -> None:
    registry = _mode_c(org_a={"default": True}, org_b={"aliases": ["default"], "enabled": False})
    assert registry.legacy_default_alias() is None


def test_default_alias_is_unique() -> None:
    with pytest.raises(OwnerConfigError, match="multiple owners"):
        _mode_c(org_a={"aliases": ["default"]}, org_b={"aliases": ["default"]})
    # Aliases are lowercase-only, so a case variant cannot sneak in a second binding
    with pytest.raises(OwnerConfigError, match="Invalid alias"):
        _mode_c(org_a={"aliases": ["default"]}, org_b={"aliases": ["Default"]})


@pytest.mark.parametrize(
    ("build", "owner_id", "expected"),
    [
        (_mode_a, "default", ["old-bot", "older-bot"]),
        (_mode_b, "default", ["old-bot", "older-bot"]),
        (_mode_b, "org-b", []),
        (_mode_c, "org-a", []),
        (_mode_c, "org-b", []),
        (_mode_c_alias_on_a_default_on_b, "org-a", ["old-bot", "older-bot"]),
        (_mode_c_alias_on_a_default_on_b, "org-b", []),
        (_mode_c_alias_and_default_on_a, "org-a", ["old-bot", "older-bot"]),
        (_mode_c_alias_and_default_on_a, "org-b", []),
    ],
)
def test_legacy_comment_authors_follow_registry_alias(build, owner_id, expected) -> None:
    alias = build().legacy_default_alias()
    assert extract_comment_author_logins_for_owner(LEGACY_AUTHORS, owner_id, alias) == expected


def test_effective_owner_id_maps_only_legacy_rows() -> None:
    assert effective_owner_id("default", "org-a") == "org-a"
    assert effective_owner_id(None, "org-a") == "org-a"
    assert effective_owner_id("default", "default") == "default"
    assert effective_owner_id("org-b", "org-a") == "org-b"


@pytest.mark.parametrize(
    ("build", "warned"),
    [
        (_mode_a, False),
        (_mode_b, False),
        (_mode_c, True),
        (_mode_c_alias_on_a_default_on_b, False),
    ],
)
def test_startup_warns_when_no_owner_bound_to_legacy_default(build, warned, caplog) -> None:
    from app.main import _validate_startup_config

    with (
        patch("app.main.get_settings") as mock_settings,
        patch("app.main.get_app_config", return_value=AppConfig()),
        patch("app.main.OwnerRegistry.build", return_value=build()),
        caplog.at_level(logging.WARNING),
    ):
        mock_settings.return_value.github_webhook_secret = ""
        _validate_startup_config()

    from app.owners.registry import LEGACY_DEFAULT_UNBOUND_WARNING

    hits = [r for r in caplog.records if r.getMessage() == LEGACY_DEFAULT_UNBOUND_WARNING]
    assert len(hits) == (1 if warned else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build", "warned"),
    [
        (_mode_a, False),
        (_mode_b, False),
        (_mode_c, True),
        (_mode_c_alias_on_a_default_on_b, False),
    ],
)
async def test_worker_startup_warns_when_no_owner_bound_to_legacy_default(build, warned, caplog) -> None:
    from app.owners.registry import LEGACY_DEFAULT_UNBOUND_WARNING
    from app.workers import settings as worker_settings

    with (
        patch.object(worker_settings, "get_settings"),
        patch.object(worker_settings, "configure_logging"),
        patch.object(worker_settings, "start_worker_metrics_server"),
        patch.object(worker_settings, "create_engine"),
        patch.object(worker_settings, "create_session_factory"),
        patch("app.owners.registry.get_owner_registry", return_value=build()),
        caplog.at_level(logging.WARNING),
    ):
        await worker_settings.startup({})

    hits = [r for r in caplog.records if r.getMessage() == LEGACY_DEFAULT_UNBOUND_WARNING]
    assert len(hits) == (1 if warned else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (_mode_a, "default"),
        (_mode_b, "default"),
        (_mode_c, None),
        (_mode_c_alias_on_a_default_on_b, "org-a"),
    ],
)
async def test_digest_open_prs_passes_legacy_default_alias(build, expected) -> None:
    from app.workers import settings as worker_settings

    registry = build()
    run_digest = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    with (
        patch.object(worker_settings, "get_app_config", return_value=AppConfig()),
        patch("app.owners.registry.get_owner_registry", return_value=registry),
        patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=MagicMock()),
        patch.object(worker_settings, "run_digest", run_digest),
    ):
        await worker_settings.digest_open_prs({"session_factory": MagicMock()})

    calls = run_digest.await_args_list
    assert {call.kwargs["owner_id"] for call in calls} == set(registry.owners)
    assert {call.kwargs["legacy_default_owner"] for call in calls} == {expected}
