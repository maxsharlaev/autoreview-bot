"""Per-owner Jira and Slack bindings (multi-owner M3, part 1).

Every test builds a real OwnerRegistry in mode B: the legacy owner 'default' has its own
Jira site and Slack channel configured globally, and named owners either bring their own
bindings or none. HTTP goes through respx, so any request to a host without a route fails.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import respx
from app.adapters.github import GitHubAppClient
from app.config import AppConfig, JiraYaml, Settings, SlackYaml
from app.models import DigestRun
from app.owners.registry import OwnerRegistry
from app.services.comment_render import RenderInput
from app.services.publisher import Publisher, empty_verified
from app.services.verifier import FindingView
from httpx import Response

LEGACY_JIRA = "https://legacy.atlassian.net"
B_JIRA = "https://b.atlassian.net"
D_JIRA = "https://d.atlassian.net"
SLACK_POST = "https://slack.com/api/chat.postMessage"
DISABLED_SECRET = "disabled-owner-secret-0123456789"


def basic(email: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


LEGACY_AUTH = basic("legacy@legacy.example", "legacy-jira-token")
B_AUTH = basic("bot@b.example", "b-jira-token")
D_AUTH = basic("bot@d.example", "d-jira-token")


def legacy_settings() -> Settings:
    return Settings.model_construct(
        github_token="ghp_legacy",
        jira_base_url=LEGACY_JIRA,
        jira_email="legacy@legacy.example",
        jira_api_token="legacy-jira-token",
        slack_bot_token="xoxb-legacy",
        openai_api_key="sk-shared",
    )


def app_config() -> AppConfig:
    return AppConfig(
        github={"allowed_repos": ["legacy-org/*"]},
        jira={
            "base_url": LEGACY_JIRA,
            "email": "legacy@legacy.example",
            "projects": {"LEG": {"rework_status": "Legacy Rework"}},
        },
        slack={"enabled": True, "channel": "#legacy"},
        owners={
            # Own Jira site with one allowed project and its own rework status, own Slack channel.
            "org-b": {
                "github": {"auth": "pat", "allowed_repos": ["org-b/*"]},
                "jira": {
                    "base_url": B_JIRA + "/",
                    "email": "bot@b.example",
                    "projects": {"BBB": {"rework_status": "Fix B"}},
                },
                "slack": {"enabled": True, "channel": "#org-b"},
            },
            # No jira, no slack: both disabled, never the legacy site or channel.
            "org-c": {"github": {"auth": "pat", "allowed_repos": ["org-c/*"]}},
            # Own Jira site (token via an explicit env name), no Slack.
            "org-d": {
                "github": {"auth": "pat", "allowed_repos": ["org-d/*"]},
                "jira": {
                    "base_url": D_JIRA,
                    "email": "bot@d.example",
                    "api_token_env": "SHARED_D_JIRA_TOKEN",
                    "projects": {"DDD": {"rework_status": "Rework D"}},
                },
            },
            "org-off": {
                "enabled": False,
                "github": {"auth": "pat", "webhook_secret_env": "ORG_OFF_WEBHOOK_SECRET"},
            },
        },
    )


ENV = {
    "OWNER_ORG_B_GITHUB_TOKEN": "ghp_b",
    "OWNER_ORG_B_JIRA_API_TOKEN": "b-jira-token",
    "OWNER_ORG_B_SLACK_BOT_TOKEN": "xoxb-b",
    "OWNER_ORG_C_GITHUB_TOKEN": "ghp_c",
    "OWNER_ORG_D_GITHUB_TOKEN": "ghp_d",
    "SHARED_D_JIRA_TOKEN": "d-jira-token",
    "ORG_OFF_WEBHOOK_SECRET": DISABLED_SECRET,
}


def build_registry() -> OwnerRegistry:
    return OwnerRegistry.build(legacy_settings(), app_config(), env=dict(ENV))


@pytest.fixture(autouse=True)
def global_legacy_integrations(monkeypatch, tmp_path):
    """Make the process-wide settings/config carry the legacy Jira site and Slack channel.

    Any code path that falls back to get_settings()/get_app_config() for a named owner
    would then reach the legacy hosts, which the tests assert never happens.
    """
    import yaml
    from app.config import get_app_config, get_settings

    config_file = tmp_path / "global-config.yaml"
    config_file.write_text(
        yaml.safe_dump(app_config().model_dump(include={"github", "jira", "slack"})), encoding="utf-8"
    )
    monkeypatch.setenv("CONFIG_PATH", str(config_file))
    monkeypatch.setenv("JIRA_BASE_URL", LEGACY_JIRA)
    monkeypatch.setenv("JIRA_EMAIL", "legacy@legacy.example")
    monkeypatch.setenv("JIRA_API_TOKEN", "legacy-jira-token")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-legacy")
    get_settings.cache_clear()
    get_app_config.cache_clear()
    yield
    get_settings.cache_clear()
    get_app_config.cache_clear()


def test_global_fallback_would_reach_legacy_integrations() -> None:
    """Guard for the isolation tests below: the legacy (global) clients are live in this module."""
    from app.adapters.jira import JiraClient
    from app.adapters.slack import SlackClient

    jira, slack = JiraClient(), SlackClient()
    assert (jira.base_url, jira.token, jira.enabled()) == (LEGACY_JIRA, "legacy-jira-token", True)
    assert (slack._channel, slack._token, slack.enabled()) == ("#legacy", "xoxb-legacy", True)


class JiraSite:
    """respx routes for one Jira site; records method, path and Authorization per request."""

    def __init__(self, router: respx.MockRouter, base_url: str, *, status: str = "In Review") -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.transition_targets: list[str] = []
        host = base_url.removeprefix("https://")

        def handler(request):
            self.calls.append((request.method, request.url.path, request.headers.get("authorization", "")))
            path = request.url.path
            if path.endswith("/comment"):
                return Response(201, json={})
            if path.endswith("/transitions") and request.method == "GET":
                names = ["In Progress", "Legacy Rework", "Fix B", "Rework D"]
                return Response(
                    200, json={"transitions": [{"id": str(i), "to": {"name": n}} for i, n in enumerate(names)]}
                )
            if path.endswith("/transitions"):
                self.transition_targets.append(names_by_id[json.loads(request.content)["transition"]["id"]])
                return Response(204)
            key = path.rsplit("/", 1)[-1]
            return Response(
                200,
                json={
                    "key": key,
                    "fields": {"summary": f"{key} summary", "status": {"name": status}, "issuetype": {"name": "Task"}},
                },
            )

        names_by_id = {str(i): n for i, n in enumerate(["In Progress", "Legacy Rework", "Fix B", "Rework D"])}
        self.route = router.route(host=host).mock(side_effect=handler)

    @property
    def authorizations(self) -> set[str]:
        return {auth for _method, _path, auth in self.calls}


class SlackApi:
    def __init__(self, router: respx.MockRouter) -> None:
        self.posts: list[tuple[str, str]] = []

        def handler(request):
            self.posts.append((request.headers["authorization"], json.loads(request.content)["channel"]))
            return Response(200, json={"ok": True})

        self.route = router.post(SLACK_POST).mock(side_effect=handler)


def _render(issue_key: str) -> RenderInput:
    verified = empty_verified(files_total=1, files_reviewed=1, skipped=[], truncated=False)
    verified.findings = [
        FindingView(
            stable_id="f1",
            severity="P1",
            confidence=0.9,
            category="correctness",
            path="src/app.py",
            line=3,
            title="Off by one",
            scenario="loop",
            evidence="i <= n",
            recommendation="use <",
            blocking_candidate=True,
        )
    ]
    return RenderInput(
        repository="org/repo",
        pr_number=7,
        head_sha="a" * 40,
        mode="advisory",
        run_id="run-1",
        issue_key=issue_key,
        jira_warning=None,
        verified=verified,
        transitions=[],
        duration_ms=1,
        model="test",
        visibility="private",
    )


async def _publish(owner_id: str, issue_key: str) -> dict[str, bool]:
    ctx = build_registry().get(owner_id)
    assert ctx is not None
    publisher = Publisher.from_context(ctx)
    return await publisher.publish(
        owner="org",
        repo="repo",
        pr_number=7,
        render=_render(issue_key),
        has_blockers=True,
        issue_key=issue_key,
        current_jira_status="In Review",
    )


@pytest.fixture
def no_github_comment(monkeypatch):
    monkeypatch.setattr(GitHubAppClient, "upsert_sticky_comment", AsyncMock())


@pytest.fixture
def http():
    with respx.mock(assert_all_called=False) as router:
        yield SimpleNamespace(
            legacy=JiraSite(router, LEGACY_JIRA),
            b=JiraSite(router, B_JIRA),
            d=JiraSite(router, D_JIRA),
            slack=SlackApi(router),
        )


# --- registry: bindings and effective config -------------------------------------------------


def test_named_owner_config_carries_only_its_own_integrations() -> None:
    registry = build_registry()
    legacy, b, c, d = (registry.get(o) for o in ("default", "org-b", "org-c", "org-d"))

    assert legacy.config.jira.base_url == LEGACY_JIRA
    assert legacy.config.slack == SlackYaml(enabled=True, channel="#legacy")

    assert b.config.jira.base_url == B_JIRA
    assert b.config.jira.email == "bot@b.example"
    assert {k: v.rework_status for k, v in b.config.jira.projects.items()} == {"BBB": "Fix B"}
    assert b.config.slack == SlackYaml(enabled=True, channel="#org-b")

    assert c.config.jira == JiraYaml()
    assert c.config.slack == SlackYaml(enabled=False, channel="")
    assert d.config.slack == SlackYaml(enabled=False, channel="")

    # Policy sections are still the global ones in this PR.
    assert b.config.public_repos == legacy.config.public_repos


def test_owner_bindings_use_only_owner_secrets() -> None:
    registry = build_registry()
    b, c, d = (registry.get(o) for o in ("org-b", "org-c", "org-d"))
    assert (b.jira.base_url, b.jira.email, b.jira.api_token, b.jira.projects) == (
        B_JIRA,
        "bot@b.example",
        "b-jira-token",
        {"BBB": "Fix B"},
    )
    assert (b.slack.channel, b.slack.bot_token) == ("#org-b", "xoxb-b")
    assert c.jira is None and c.slack is None
    assert d.jira.api_token == "d-jira-token"
    assert d.slack is None


def test_incomplete_integration_blocks_warn() -> None:
    config = AppConfig(
        owners={
            "org-x": {
                "github": {"auth": "pat", "allowed_repos": ["org-x/*"]},
                "jira": {"projects": {"XXX": {}}},
                "slack": {"enabled": True},
            }
        }
    )
    env = {"OWNER_ORG_X_GITHUB_TOKEN": "ghp_x", "OWNER_ORG_X_SLACK_BOT_TOKEN": "xoxb-x"}
    registry = OwnerRegistry.build(Settings.model_construct(), config, env=env)
    assert registry.get("org-x").jira is None
    assert any("jira block without base_url and email" in w for w in registry.warnings)
    assert any("Slack enabled but no channel" in w for w in registry.warnings)


# --- publisher: Jira site, auth, projects, rework status and Slack per owner ------------------


@pytest.mark.asyncio
async def test_owner_jira_and_slack_go_to_own_site_auth_and_channel(http, no_github_comment) -> None:
    result = await _publish("org-b", "BBB-7")

    assert result == {"github_comment": True, "jira_comment": True, "jira_transition": True, "slack": True}
    assert [(m, p) for m, p, _ in http.b.calls] == [
        ("POST", "/rest/api/2/issue/BBB-7/comment"),
        ("GET", "/rest/api/3/issue/BBB-7/transitions"),
        ("POST", "/rest/api/3/issue/BBB-7/transitions"),
    ]
    assert http.b.authorizations == {B_AUTH}
    assert http.b.transition_targets == ["Fix B"]
    assert http.slack.posts == [("Bearer xoxb-b", "#org-b")]
    assert http.legacy.calls == [] and http.d.calls == []


@pytest.mark.asyncio
async def test_second_owner_site_and_rework_status(http, no_github_comment) -> None:
    result = await _publish("org-d", "DDD-3")

    assert result["jira_comment"] and result["jira_transition"]
    assert {(m, p) for m, p, _ in http.d.calls} >= {("POST", "/rest/api/2/issue/DDD-3/comment")}
    assert http.d.authorizations == {D_AUTH}
    assert http.d.transition_targets == ["Rework D"]
    # org-d has no slack block: nothing is posted, not even to the legacy channel.
    assert http.slack.posts == []
    assert http.legacy.calls == [] and http.b.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner_id", "issue_key"), [("org-b", "LEG-1"), ("org-b", "DDD-3"), ("org-d", "BBB-7")])
async def test_owner_jira_only_in_own_allowed_projects(http, no_github_comment, owner_id, issue_key) -> None:
    result = await _publish(owner_id, issue_key)

    assert not result["jira_comment"] and not result["jira_transition"]
    assert http.legacy.calls == [] and http.b.calls == [] and http.d.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("issue_key", ["LEG-1", "BBB-7"])
async def test_owner_without_jira_and_slack_makes_no_calls(http, no_github_comment, issue_key) -> None:
    result = await _publish("org-c", issue_key)

    assert result == {"github_comment": True, "jira_comment": False, "jira_transition": False, "slack": False}
    assert http.legacy.route.call_count == http.b.route.call_count == http.d.route.call_count == 0
    assert http.slack.route.call_count == 0


@pytest.mark.asyncio
async def test_legacy_owner_keeps_global_jira_and_slack(http, no_github_comment) -> None:
    result = await _publish("default", "LEG-1")

    assert result == {"github_comment": True, "jira_comment": True, "jira_transition": True, "slack": True}
    assert http.legacy.authorizations == {LEGACY_AUTH}
    assert http.legacy.transition_targets == ["Legacy Rework"]
    assert http.slack.posts == [("Bearer xoxb-legacy", "#legacy")]
    assert http.b.calls == [] and http.d.calls == []


# --- digest: one DigestRun and Slack post per owner with its own binding ----------------------


class _DigestSession:
    def __init__(self, added: list) -> None:
        self.added = added

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_digest_posts_per_owner_with_own_token_and_channel(http) -> None:
    from app.workers import settings as worker_settings

    registry = build_registry()
    added: list = []

    async def payload(_session, *, owner_id, legacy_default_owner):
        return {"open_pr_count": 1, "blocker_pr_count": 0, "items": [], "generated_at": owner_id}

    with (
        patch.object(worker_settings, "get_app_config", return_value=app_config()),
        patch("app.owners.registry.get_owner_registry", return_value=registry),
        patch("app.services.digest.build_digest_payload", side_effect=payload),
    ):
        result = await worker_settings.digest_open_prs({"session_factory": lambda: _DigestSession(added)})

    # Wildcard-only allowlists: no GitHub refresh; Slack is the only HTTP traffic.
    assert sorted(http.slack.posts) == sorted([("Bearer xoxb-legacy", "#legacy"), ("Bearer xoxb-b", "#org-b")])
    assert http.slack.route.call_count == 2
    digests = {d.owner_id: d for d in added if isinstance(d, DigestRun)}
    assert set(digests) == {"default", "org-b", "org-c", "org-d"}
    assert {o: d.slack_sent for o, d in digests.items()} == {
        "default": True,
        "org-b": True,
        "org-c": False,
        "org-d": False,
    }
    assert len(result.split(",")) == 4


@pytest.mark.asyncio
async def test_mode_a_digest_posts_once_to_legacy_channel(http) -> None:
    from app.workers import settings as worker_settings

    config = app_config().model_copy(update={"owners": {}})
    registry = OwnerRegistry.build(legacy_settings(), config, env={})
    added: list = []

    async def payload(_session, *, owner_id, legacy_default_owner):
        return {"open_pr_count": 0, "blocker_pr_count": 0, "items": [], "generated_at": owner_id}

    with (
        patch.object(worker_settings, "get_app_config", return_value=config),
        patch("app.owners.registry.get_owner_registry", return_value=registry),
        patch("app.services.digest.build_digest_payload", side_effect=payload),
    ):
        await worker_settings.digest_open_prs({"session_factory": lambda: _DigestSession(added)})

    assert http.slack.posts == [("Bearer xoxb-legacy", "#legacy")]
    assert [d.owner_id for d in added if isinstance(d, DigestRun)] == ["default"]


# --- webhook: disabled owner path verifies the signature first --------------------------------


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_disabled(path_owner: str, headers: dict[str, str], registry: OwnerRegistry | None = None):
    from unittest.mock import MagicMock

    from app.api.v1.pull_request import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = MagicMock()
    app.state.redis = MagicMock()
    with patch("app.api.v1.pull_request.get_owner_registry", return_value=registry or build_registry()):
        return TestClient(app).post(
            f"/pull-request/{path_owner}", content=BODY, headers={"X-GitHub-Event": "ping", **headers}
        )


BODY = b'{"zen": "x"}'


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Hub-Signature-256": "sha256=invalid"},
        {"X-Hub-Signature-256": _signature("some-other-owner-secret-123456", BODY)},
    ],
    ids=["unsigned", "garbage", "foreign-secret"],
)
def test_disabled_owner_path_rejects_bad_signature(headers) -> None:
    response = _post_disabled("org-off", headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid signature"}


def test_disabled_owner_path_with_valid_signature_reports_disabled() -> None:
    response = _post_disabled("org-off", {"X-Hub-Signature-256": _signature(DISABLED_SECRET, BODY)})
    assert response.status_code == 202
    assert response.json() == {"status": "skipped", "reason": "owner_disabled"}


def test_disabled_owner_without_secret_always_401() -> None:
    config = AppConfig(
        owners={
            "org-a": {"default": True, "github": {"auth": "pat"}},
            "org-off": {"enabled": False, "github": {"auth": "pat"}},
        }
    )
    registry = OwnerRegistry.build(Settings.model_construct(), config, env={"OWNER_ORG_A_GITHUB_TOKEN": "ghp_a"})
    response = _post_disabled("org-off", {"X-Hub-Signature-256": _signature("", BODY)}, registry)
    assert response.status_code == 401


def test_unknown_owner_path_still_404() -> None:
    assert _post_disabled("nobody", {}).status_code == 404


# --- owners block with only disabled owners (no active owner at all) ---

ONLY_OFF_SECRET = "only-off-owner-secret-0123456789"


def only_disabled_registry() -> OwnerRegistry:
    config = AppConfig(
        owners={
            "org-off": {
                "enabled": False,
                "aliases": ["off-alias"],
                "github": {"auth": "pat", "webhook_secret_env": "ONLY_OFF_WEBHOOK_SECRET", "allowed_repos": ["off/*"]},
            }
        }
    )
    return OwnerRegistry.build(Settings.model_construct(), config, env={"ONLY_OFF_WEBHOOK_SECRET": ONLY_OFF_SECRET})


def test_only_disabled_owners_keep_aliases_and_config() -> None:
    registry = only_disabled_registry()
    assert registry.owners == {}
    assert registry.aliases == {"off-alias": "org-off"}
    assert registry.disabled_owners == {"org-off"}
    assert registry.get_allowed_repos_for_owner("org-off") == ["off/*"]
    for selector in ("org-off", "ORG-OFF", "Off-Alias", "off-alias", "OFF-ALIAS"):
        assert registry.is_disabled(selector)
        assert registry.disabled_owner_webhook_secret(selector) == ONLY_OFF_SECRET
        assert registry.resolve("off/repo", explicit_owner=selector).reason == "owner_disabled"
    assert not registry.is_disabled("nobody")


@pytest.mark.parametrize("path_owner", ["org-off", "ORG-OFF", "Off-Alias", "off-alias", "OFF-ALIAS"])
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Hub-Signature-256": "sha256=invalid"},
        {"X-Hub-Signature-256": _signature("some-other-owner-secret-123456", BODY)},
    ],
    ids=["unsigned", "garbage", "foreign-secret"],
)
def test_only_disabled_owners_path_rejects_bad_signature(path_owner, headers) -> None:
    response = _post_disabled(path_owner, headers, only_disabled_registry())
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid signature"}


@pytest.mark.parametrize("path_owner", ["org-off", "ORG-OFF", "Off-Alias", "off-alias", "OFF-ALIAS"])
def test_only_disabled_owners_path_with_valid_signature_reports_disabled(path_owner) -> None:
    headers = {"X-Hub-Signature-256": _signature(ONLY_OFF_SECRET, BODY)}
    response = _post_disabled(path_owner, headers, only_disabled_registry())
    assert response.status_code == 202
    assert response.json() == {"status": "skipped", "reason": "owner_disabled"}


def test_only_disabled_owners_unknown_path_still_404() -> None:
    assert _post_disabled("nobody", {}, only_disabled_registry()).status_code == 404


@pytest.mark.parametrize("selector", ["org-off", "Off-Alias", "off-alias", "OFF-ALIAS"])
@pytest.mark.parametrize("via", ["header", "query"])
def test_only_disabled_owners_api_selector_reports_disabled(selector, via) -> None:
    from unittest.mock import MagicMock

    from app.api.deps import get_session
    from app.api.v1.reviews import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    app.state.redis = MagicMock()

    async def _no_session():
        yield MagicMock()

    app.dependency_overrides[get_session] = _no_session
    registry = only_disabled_registry()
    headers = {"X-Api-Key": "operator-key-0123456789"}
    params = {}
    if via == "header":
        headers["X-Review-Owner"] = selector
    else:
        params["owner"] = selector
    with (
        patch("app.api.deps.get_settings", return_value=Settings.model_construct(review_api_key=headers["X-Api-Key"])),
        patch("app.api.deps.get_owner_registry", return_value=registry),
        patch("app.api.v1.reviews.get_owner_registry", return_value=registry),
    ):
        response = TestClient(app).post(
            "/reviews", params=params, headers=headers, json={"repository": "off/repo", "number": 1}
        )
    assert response.status_code == 403
    assert response.json() == {"detail": "owner_disabled"}
