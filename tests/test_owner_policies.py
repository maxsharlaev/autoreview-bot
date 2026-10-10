"""Per-owner policy overrides and model settings (multi-owner M3, part 2).

Every test builds a real OwnerRegistry in mode B: the legacy owner 'default' uses the global
sections, org-a overrides most policies and its model, org-b overrides nothing, org-c only
its model (key by naming convention). HTTP goes through respx.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import respx
from app.adapters.github import GitHubAppClient, PullRequestInfo
from app.config import AppConfig, Settings
from app.models import DigestRun
from app.owners.registry import OwnerConfigError, OwnerRegistry
from app.services.publisher import Publisher
from app.services.size_guard import classify_pr_size
from tests.test_owner_integrations import JiraSite, SlackApi, _DigestSession, _render

A_JIRA = "https://a.atlassian.net"
B_JIRA = "https://b.atlassian.net"
A_SECRET = "org-a-webhook-secret-0123456789"
B_SECRET = "org-b-webhook-secret-0123456789"
LEGACY_SECRET = "legacy-webhook-secret-0123456789"

SECRETS = {
    "OWNER_ORG_A_GITHUB_TOKEN": "ghp_a",
    "OWNER_ORG_B_GITHUB_TOKEN": "ghp_b",
    "OWNER_ORG_C_GITHUB_TOKEN": "ghp_c",
    "OWNER_ORG_A_GITHUB_WEBHOOK_SECRET": A_SECRET,
    "OWNER_ORG_B_GITHUB_WEBHOOK_SECRET": B_SECRET,
    "OWNER_ORG_A_JIRA_API_TOKEN": "a-jira-token",
    "OWNER_ORG_B_JIRA_API_TOKEN": "b-jira-token",
    "OWNER_ORG_A_SLACK_BOT_TOKEN": "xoxb-a",
    "OWNER_ORG_B_SLACK_BOT_TOKEN": "xoxb-b",
    "ORG_A_MODEL_KEY": "sk-org-a",
    "OWNER_ORG_C_OPENAI_API_KEY": "sk-org-c",
}
SHARED_MODEL_KEY = "sk-shared"


def policy_settings() -> Settings:
    return Settings.model_construct(
        github_token="ghp_legacy",
        github_webhook_secret=LEGACY_SECRET,
        slack_bot_token="xoxb-legacy",
        openai_api_key=SHARED_MODEL_KEY,
    )


def policy_config() -> AppConfig:
    return AppConfig(
        github={"allowed_repos": ["legacy-org/*"]},
        slack={"enabled": True, "channel": "#legacy"},
        codex={"model": "global-model", "reasoning_effort": "medium"},
        owners={
            "org-a": {
                "github": {"auth": "pat", "allowed_repos": ["org-a/*"]},
                "jira": {"base_url": A_JIRA, "email": "bot@a.example", "projects": {"AAA": {}}},
                "slack": {"enabled": True, "channel": "#org-a"},
                "public_repos": {"security_findings": "redact_all"},
                "language": {"summary": "ru", "details": "ru"},
                "features": {"jira_comment": False, "jira_transition": False, "digest_enabled": False},
                "pr_description": {"enabled": True, "mode": "fill_empty"},
                "size_guard": {"soft": {"commits": 2}, "hard": {"commits": 3}, "override_label": "a:force"},
                "codex": {"model": "model-a", "reasoning_effort": "high", "api_key_env": "ORG_A_MODEL_KEY"},
            },
            "org-b": {
                "github": {"auth": "pat", "allowed_repos": ["org-b/*"]},
                "jira": {"base_url": B_JIRA, "email": "bot@b.example", "projects": {"BBB": {}}},
                "slack": {"enabled": True, "channel": "#org-b"},
            },
            "org-c": {
                "github": {"auth": "pat", "allowed_repos": ["org-c/*"]},
                "codex": {"model": "model-c"},
            },
        },
    )


def policy_registry(config: AppConfig | None = None, env: dict[str, str] | None = None) -> OwnerRegistry:
    return OwnerRegistry.build(policy_settings(), config or policy_config(), env=dict(SECRETS if env is None else env))


# --- effective config --------------------------------------------------------------------------


def test_policy_overrides_deep_merge_over_global_sections() -> None:
    config = policy_config()
    registry = policy_registry(config)
    a = registry.get("org-a").config

    assert (a.size_guard.soft.commits, a.size_guard.soft.changed_lines) == (2, config.size_guard.soft.changed_lines)
    assert (a.size_guard.hard.commits, a.size_guard.hard.changed_lines) == (3, config.size_guard.hard.changed_lines)
    assert a.size_guard.override_label == "a:force"
    assert (a.public_repos.security_findings, a.public_repos.jira_disclosure) == (
        "redact_all",
        config.public_repos.jira_disclosure,
    )
    assert (a.language.summary, a.language.details) == ("ru", "ru")
    assert (a.features.jira_comment, a.features.jira_transition, a.features.digest_enabled) == (False, False, False)
    assert (a.pr_description.enabled, a.pr_description.mode) == (True, "fill_empty")
    assert a.pr_description.timeout_seconds == config.pr_description.timeout_seconds
    assert (a.codex.model, a.codex.reasoning_effort) == ("model-a", "high")
    assert a.codex.model_dump(exclude={"model", "reasoning_effort"}) == config.codex.model_dump(
        exclude={"model", "reasoning_effort"}
    )

    b = registry.get("org-b").config
    for section in ("public_repos", "language", "features", "pr_description", "size_guard", "codex", "schedule"):
        assert getattr(b, section) == getattr(config, section), section
    c = registry.get("org-c").config
    assert (c.codex.model, c.codex.reasoning_effort) == ("model-c", "medium")

    assert registry.get("default").config == config
    assert not any("override" in warning for warning in registry.warnings)


def test_mode_a_effective_config_is_the_global_config() -> None:
    config = policy_config().model_copy(update={"owners": {}})
    registry = OwnerRegistry.build(policy_settings(), config, env={})
    assert list(registry.owners) == ["default"]
    assert registry.get("default").config is config
    assert registry.get("default").openai_api_key == SHARED_MODEL_KEY


def test_invalid_merged_override_fails_startup_without_values() -> None:
    config = policy_config()
    config.owners["org-b"]["size_guard"] = {"soft": {"commits": 500}}
    with pytest.raises(OwnerConfigError, match="size_guard override for owner 'org-b'") as exc:
        policy_registry(config)
    assert "ghp_" not in str(exc.value) and "sk-" not in str(exc.value)


def test_unknown_override_field_is_rejected() -> None:
    config = policy_config()
    config.owners["org-b"]["codex"] = {"sandbox": "danger-full-access"}
    with pytest.raises(OwnerConfigError, match="org-b"):
        policy_registry(config)


# --- model key -------------------------------------------------------------------------------


def test_model_key_per_owner_with_shared_fallback() -> None:
    registry = policy_registry()
    assert registry.get("org-a").openai_api_key == "sk-org-a"  # codex.api_key_env
    assert registry.get("org-c").openai_api_key == "sk-org-c"  # OWNER_ORG_C_OPENAI_API_KEY
    assert registry.get("org-b").openai_api_key == SHARED_MODEL_KEY
    assert registry.get("default").openai_api_key == SHARED_MODEL_KEY


def test_missing_explicit_model_key_env_fails_startup() -> None:
    env = {k: v for k, v in SECRETS.items() if k != "ORG_A_MODEL_KEY"}
    with pytest.raises(OwnerConfigError, match="ORG_A_MODEL_KEY.*org-a"):
        policy_registry(env=env)


# --- size guard --------------------------------------------------------------------------------


def _pr(commits: int, labels: tuple[str, ...] = ()) -> PullRequestInfo:
    return PullRequestInfo(
        owner="o",
        repo="r",
        number=1,
        full_name="o/r",
        html_url="",
        title="t",
        body="",
        state="open",
        draft=False,
        author="a",
        assignee=None,
        base_sha="b" * 40,
        head_sha="c" * 40,
        base_ref="main",
        head_ref="f",
        is_fork=False,
        commits_count=commits,
        additions=1,
        deletions=1,
        changed_files=1,
        labels=labels,
    )


@pytest.mark.parametrize(
    ("owner_id", "commits", "labels", "decision"),
    [
        ("org-a", 2, (), "normal"),
        ("org-a", 3, (), "soft"),
        ("org-a", 4, (), "hard"),
        ("org-a", 4, ("A:Force",), "normal"),
        ("org-a", 4, ("autoreview:force",), "hard"),
        ("org-b", 4, (), "normal"),
        ("org-b", 151, (), "hard"),
        ("org-b", 151, ("a:force",), "hard"),
        ("org-b", 151, ("autoreview:force",), "normal"),
    ],
)
def test_size_guard_thresholds_and_label_per_owner(owner_id, commits, labels, decision) -> None:
    guard = policy_registry().get(owner_id).config.size_guard
    assert classify_pr_size(_pr(commits, labels), guard) == decision


# --- Jira features -----------------------------------------------------------------------------


@pytest.fixture
def http():
    with respx.mock(assert_all_called=False) as router:
        yield SimpleNamespace(a=JiraSite(router, A_JIRA), b=JiraSite(router, B_JIRA), slack=SlackApi(router))


@pytest.mark.asyncio
async def test_owner_features_off_make_no_jira_comment_or_transition(http, monkeypatch) -> None:
    monkeypatch.setattr(GitHubAppClient, "upsert_sticky_comment", AsyncMock())
    registry = policy_registry()

    async def publish(owner_id: str, issue_key: str) -> dict[str, bool]:
        return await Publisher.from_context(registry.get(owner_id)).publish(
            owner="o",
            repo="r",
            pr_number=7,
            render=_render(issue_key),
            has_blockers=True,
            issue_key=issue_key,
            current_jira_status="In Review",
        )

    result_a = await publish("org-a", "AAA-7")
    assert http.a.calls == []
    assert (result_a["jira_comment"], result_a["jira_transition"]) == (False, False)
    assert http.slack.posts == [("Bearer xoxb-a", "#org-a")]

    result_b = await publish("org-b", "BBB-7")
    assert [(m, p) for m, p, _ in http.b.calls] == [
        ("POST", "/rest/api/2/issue/BBB-7/comment"),
        ("GET", "/rest/api/3/issue/BBB-7/transitions"),
        ("POST", "/rest/api/3/issue/BBB-7/transitions"),
    ]
    assert (result_b["jira_comment"], result_b["jira_transition"]) == (True, True)


# --- root webhook: override label per owner ----------------------------------------------------


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _labeled_payload(full_name: str, label: str) -> bytes:
    pr = {
        "number": 7,
        "draft": False,
        "head": {"sha": "c" * 40, "repo": {"full_name": full_name, "fork": False}},
        "base": {"sha": "b" * 40, "repo": {"full_name": full_name}},
    }
    return json.dumps(
        {"action": "labeled", "label": {"name": label}, "repository": {"full_name": full_name}, "pull_request": pr}
    ).encode()


def _post_webhook(registry: OwnerRegistry, path: str, body: bytes, secret: str, monkeypatch):
    from unittest.mock import MagicMock

    from app.api.deps import get_session
    from app.api.v1 import pull_request as webhook
    from app.security import webhook_config
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    for name in ("_webhook_enabled", "_normalized_secret", "_owner_webhook_secrets", "_is_legacy_single_owner"):
        monkeypatch.setattr(webhook_config, name, getattr(webhook_config, name))
    webhook_config.set_multi_owner_webhook_config(registry)

    queued: list[dict] = []

    async def fake_queue(_session, _redis, **kwargs):
        queued.append(kwargs)
        return SimpleNamespace(status="queued", id=uuid.uuid4())

    async def no_session():
        yield None

    app = FastAPI()
    app.include_router(webhook.router)
    app.state.redis = MagicMock()
    app.dependency_overrides[get_session] = no_session
    with (
        patch.object(webhook, "get_owner_registry", return_value=registry),
        patch.object(webhook, "queue_review", side_effect=fake_queue),
    ):
        response = TestClient(app).post(
            path,
            content=body,
            headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(secret, body)},
        )
    assert response.status_code == 202, response.text
    return response.json(), queued


@pytest.mark.parametrize(
    ("full_name", "secret", "label", "outcome"),
    [
        ("org-a/app", A_SECRET, "a:force", "org-a"),
        ("org-a/app", A_SECRET, "A:FORCE", "org-a"),
        ("org-a/app", A_SECRET, "autoreview:force", "ignored_label"),
        ("org-b/app", B_SECRET, "autoreview:force", "org-b"),
        ("org-b/app", B_SECRET, "a:force", "ignored_label"),
        ("org-b/app", B_SECRET, "other", "ignored_label"),
        ("legacy-org/app", LEGACY_SECRET, "autoreview:force", "default"),
        ("legacy-org/app", LEGACY_SECRET, "a:force", "ignored_label"),
    ],
)
def test_root_webhook_uses_routed_owner_override_label(full_name, secret, label, outcome, monkeypatch) -> None:
    result, queued = _post_webhook(
        policy_registry(), "/pull-request", _labeled_payload(full_name, label), secret, monkeypatch
    )
    if outcome == "ignored_label":
        assert result == {"status": "skipped", "reason": "ignored_label"}
        assert queued == []
    else:
        assert result["owner"] == outcome
        assert [(q["owner_id"], q["full_name"], q["force"]) for q in queued] == [(outcome, full_name, True)]


@pytest.mark.parametrize(("label", "queued_count"), [("a:force", 1), ("autoreview:force", 0)])
def test_owner_path_webhook_uses_owner_override_label(label, queued_count, monkeypatch) -> None:
    _result, queued = _post_webhook(
        policy_registry(), "/pull-request/org-a", _labeled_payload("org-a/app", label), A_SECRET, monkeypatch
    )
    assert len(queued) == queued_count


@pytest.mark.parametrize(("label", "outcome"), [("autoreview:force", "default"), ("a:force", "ignored_label")])
def test_root_webhook_mode_a_label_unchanged(label, outcome, monkeypatch) -> None:
    config = policy_config().model_copy(update={"owners": {}})
    registry = OwnerRegistry.build(policy_settings(), config, env={})
    result, queued = _post_webhook(
        registry, "/pull-request", _labeled_payload("legacy-org/app", label), LEGACY_SECRET, monkeypatch
    )
    if outcome == "ignored_label":
        assert result == {"status": "skipped", "reason": "ignored_label"} and queued == []
    else:
        assert [q["owner_id"] for q in queued] == ["default"]


# --- digest ------------------------------------------------------------------------------------


async def _run_digest(registry: OwnerRegistry, config: AppConfig) -> tuple[str, list]:
    from app.workers import settings as worker_settings

    added: list = []

    async def payload(_session, *, owner_id, legacy_default_owner):
        return {"open_pr_count": 1, "blocker_pr_count": 0, "items": [], "generated_at": owner_id}

    with (
        patch.object(worker_settings, "get_app_config", return_value=config),
        patch("app.owners.registry.get_owner_registry", return_value=registry),
        patch("app.services.digest.build_digest_payload", side_effect=payload),
    ):
        result = await worker_settings.digest_open_prs({"session_factory": lambda: _DigestSession(added)})
    return result, [d for d in added if isinstance(d, DigestRun)]


@pytest.mark.asyncio
async def test_digest_skips_owner_with_digest_disabled(http) -> None:
    result, digests = await _run_digest(policy_registry(), policy_config())

    assert sorted(d.owner_id for d in digests) == ["default", "org-b", "org-c"]
    assert sorted(http.slack.posts) == [("Bearer xoxb-b", "#org-b"), ("Bearer xoxb-legacy", "#legacy")]
    assert len(result.split(",")) == 3


@pytest.mark.asyncio
async def test_digest_runs_for_owner_enabled_over_disabled_global(http) -> None:
    config = policy_config()
    config.features.digest_enabled = False
    config.owners["org-b"]["features"] = {"digest_enabled": True}

    result, digests = await _run_digest(policy_registry(config), config)

    assert [d.owner_id for d in digests] == ["org-b"]
    assert http.slack.posts == [("Bearer xoxb-b", "#org-b")]
    assert result == str(digests[0].id)


@pytest.mark.asyncio
async def test_mode_a_digest_disabled_globally_stays_disabled(http) -> None:
    config = policy_config().model_copy(update={"owners": {}})
    config.features.digest_enabled = False
    result, digests = await _run_digest(OwnerRegistry.build(policy_settings(), config, env={}), config)
    assert (result, digests, http.slack.posts) == ("disabled", [], [])


@pytest.mark.parametrize(
    ("global_enabled", "owner_b_override", "expected"),
    [(True, None, True), (False, None, False), (False, True, True)],
)
def test_digest_cron_registered_when_any_owner_has_digest(global_enabled, owner_b_override, expected) -> None:
    from app.workers import settings as worker_settings

    config = policy_config()
    config.features.digest_enabled = global_enabled
    if owner_b_override is not None:
        config.owners["org-b"]["features"] = {"digest_enabled": owner_b_override}
    registry = policy_registry(config)
    with (
        patch.object(worker_settings, "get_app_config", return_value=config),
        patch("app.owners.registry.get_owner_registry", return_value=registry),
    ):
        jobs = worker_settings._cron_jobs()
    assert (len(jobs) == 1) is expected
