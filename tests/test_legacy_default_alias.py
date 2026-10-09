"""Which owner inherits rows stored with the legacy owner_id 'default'."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import AppConfig
from app.models import effective_owner_id, extract_comment_author_logins_for_owner
from app.owners.registry import OwnerRegistry
from tests.test_multi_owner_m2_bugs import _mock_settings

LEGACY_AUTHORS = ["old-bot", {"login": "older-bot", "owner_id": "default", "kind": None}]


def _mode_a() -> OwnerRegistry:
    return OwnerRegistry.build(_mock_settings(github_token="ghp_legacy"), AppConfig(), env={})


def _mode_b() -> OwnerRegistry:
    config = AppConfig(owners={"org-b": {"default": True, "github": {"auth": "pat"}}})
    return OwnerRegistry.build(
        _mock_settings(github_token="ghp_legacy"), config, env={"OWNER_ORG_B_GITHUB_TOKEN": "ghp_b"}
    )


def _mode_c(**org_b_extra) -> OwnerRegistry:
    config = AppConfig(
        owners={
            "org-a": {"default": True, "github": {"auth": "pat"}},
            "org-b": {"github": {"auth": "pat"}, **org_b_extra},
        }
    )
    env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_a", "OWNER_ORG_B_GITHUB_TOKEN": "ghp_b"}
    return OwnerRegistry.build(_mock_settings(), config, env=env)


def test_mode_a_legacy_rows_stay_with_default() -> None:
    registry = _mode_a()
    assert registry.default_owner_id == "default"
    assert registry.legacy_default_alias() is None


def test_mode_b_named_default_owner_does_not_inherit_legacy_rows() -> None:
    registry = _mode_b()
    assert registry.default_owner_id == "org-b"
    assert registry.get("default") is not None
    assert registry.legacy_default_alias() is None


def test_mode_c_default_owner_inherits_legacy_rows() -> None:
    registry = _mode_c()
    assert registry.get("default") is None
    assert registry.legacy_default_alias() == "org-a"


def test_mode_c_explicit_default_alias_wins() -> None:
    assert _mode_c(aliases=["default"]).legacy_default_alias() == "org-b"


@pytest.mark.parametrize(
    ("build", "owner_id", "expected"),
    [
        (_mode_a, "default", ["old-bot", "older-bot"]),
        (_mode_b, "default", ["old-bot", "older-bot"]),
        (_mode_b, "org-b", []),
        (_mode_c, "org-a", ["old-bot", "older-bot"]),
        (_mode_c, "org-b", []),
    ],
)
def test_legacy_comment_authors_follow_registry_alias(build, owner_id, expected) -> None:
    alias = build().legacy_default_alias()
    assert extract_comment_author_logins_for_owner(LEGACY_AUTHORS, owner_id, alias) == expected


def test_effective_owner_id_maps_only_legacy_rows() -> None:
    assert effective_owner_id("default", "org-a") == "org-a"
    assert effective_owner_id(None, "org-a") == "org-a"
    assert effective_owner_id("default", None) == "default"
    assert effective_owner_id("org-b", "org-a") == "org-b"


@pytest.mark.asyncio
@pytest.mark.parametrize(("build", "expected"), [(_mode_a, None), (_mode_b, None), (_mode_c, "org-a")])
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
