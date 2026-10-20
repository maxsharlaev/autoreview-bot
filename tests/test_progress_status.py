"""Progress status (issue #7): check runs, commit statuses, reactions, finalization and disclosure."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.adapters.github import GitHubError, PullRequestInfo
from app.config import AppConfig, ProgressStatusYaml, PublicReposYaml
from app.owners.registry import REJECT_REPO_NOT_ALLOWED, RouteResult
from app.services import orchestrator
from app.services import progress_status as ps
from app.services.comment_render import FindingTransitionView, review_has_sensitive_content
from app.services.constants import HEAD_CHANGED, SKIP_DRAFT, SKIP_FORK, SKIP_REPO, SKIP_TOO_LARGE
from app.services.orchestrator import run_review
from app.services.review_enqueue import _report_cancelled
from app.services.verifier import FindingView, VerifiedReview

HEAD = "a" * 40
COMMENT_URL = "https://github.com/org/repo/pull/7#issuecomment-1"
SECRET_SUMMARY = "Per the Jira task, the SECRET-CRITERIA flow is broken"
SECURITY_TITLE = "SQL injection in login handler"


class FakeGitHub:
    """Records status calls; optionally fails or hangs on every status call."""

    def __init__(self, *, info: PullRequestInfo | None = None, fail: bool = False, hang: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail
        self.hang = hang
        self.info = info
        self.get_pull_request = AsyncMock(return_value=info)
        self.collaborator_permission = AsyncMock(return_value="write")
        self.list_files = AsyncMock(return_value=[])
        self.installation_token = AsyncMock(return_value="token")
        self.upsert_sticky_comment = AsyncMock(return_value=COMMENT_URL)

    async def _maybe_fail(self) -> None:
        if self.hang:
            await asyncio.sleep(10)
        if self.fail:
            raise GitHubError("403 Resource not accessible by integration", status_code=403)

    async def create_check_run(self, owner, repo, payload):
        self.calls.append(("create", payload))
        await self._maybe_fail()
        return 101

    async def update_check_run(self, owner, repo, check_run_id, payload):
        self.calls.append(("update", check_run_id, payload))
        await self._maybe_fail()

    async def create_commit_status(self, owner, repo, sha, payload):
        self.calls.append(("status", sha, payload))
        await self._maybe_fail()

    async def add_issue_reaction(self, owner, repo, number, content):
        self.calls.append(("react", number, content))
        await self._maybe_fail()
        return 555

    async def delete_issue_reaction(self, owner, repo, number, reaction_id):
        self.calls.append(("unreact", number, reaction_id))
        await self._maybe_fail()

    def payloads(self, kind: str) -> list[dict]:
        return [call[-1] for call in self.calls if call[0] == kind]

    def check_states(self) -> list[str]:
        return [call[-1]["status"] for call in self.calls if call[0] in {"create", "update"}]


def _info(visibility: str = "private", *, draft: bool = False) -> PullRequestInfo:
    return PullRequestInfo(
        number=7,
        title="ABC-12 validate input",
        body="",
        html_url="https://github.com/org/repo/pull/7",
        author="author",
        assignee=None,
        state="open",
        draft=draft,
        is_fork=False,
        base_sha="b" * 40,
        head_sha=HEAD,
        head_ref="feature/ABC-12-validation",
        base_ref="main",
        owner="org",
        repo="repo",
        full_name="org/repo",
        visibility=visibility,
    )


def _finding(severity: str = "P2", *, security: bool = False) -> FindingView:
    return FindingView(
        stable_id=f"f-{severity}-{security}",
        severity=severity,
        confidence=0.9,
        category="security" if security else "correctness",
        path="src/app.py",
        line=3,
        title=SECURITY_TITLE if security else "Off-by-one in pagination",
        scenario="A crafted username reaches the query." if security else "Last page is dropped.",
        evidence="query built with f-string" if security else "range(n - 1)",
        recommendation="Use bound parameters." if security else "Use range(n).",
        blocking_candidate=severity in {"P0", "P1"},
    )


def _verified(findings: list[FindingView], *, summary: str = "Looks reasonable.", unmet=None) -> VerifiedReview:
    return VerifiedReview(
        summary=summary,
        task_alignment_status="unclear",
        issue_key="ABC-12",
        unmet_acceptance_criteria=list(unmet or []),
        findings=findings,
        previous_findings=[],
        coverage={"files_total": 1, "files_reviewed": 1, "truncated": False, "skipped_paths": []},
    )


class Harness:
    """Runs the real run_review with external steps (clone, model, DB) replaced by fakes."""

    def __init__(
        self,
        monkeypatch,
        tmp_path,
        *,
        kind: str = "app",
        visibility: str = "private",
        config: AppConfig | None = None,
        verified: VerifiedReview | None = None,
        github: FakeGitHub | None = None,
        codex_error: BaseException | None = None,
        draft: bool = False,
        size_metrics: dict | None = None,
        jira_issue=None,
    ) -> None:
        self.config = config or AppConfig()
        self.stored: dict | None = None
        self.github = github or FakeGitHub(info=_info(visibility, draft=draft))
        if self.github.info is None:
            self.github.info = _info(visibility, draft=draft)
            self.github.get_pull_request.return_value = self.github.info
        repository = SimpleNamespace(full_name="org/repo", owner_id="org-a", comment_authors=[])
        self.pr = SimpleNamespace(
            id=uuid.uuid4(), repository=repository, number=7, findings=[], bot_title=None, issue_key=None
        )
        summary: dict = {"repository_visibility": visibility}
        if size_metrics:
            summary["size_metrics"] = size_metrics
        self.run = SimpleNamespace(
            id=uuid.uuid4(),
            pull_request=self.pr,
            status="pending",
            head_sha=HEAD,
            base_sha="b" * 40,
            summary=summary,
            trigger="webhook",
            owner_id="org-a",
            error_code=None,
            skip_reason=None,
            duration_ms=None,
            progress_status=None,
            comment_url=None,
            has_sensitive_content=None,
        )
        self.session = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    scalar_one=lambda: self.run,
                    scalar_one_or_none=lambda: None,
                    # locked_state(): the row as "stored"; tests can change self.stored to
                    # simulate a concurrent writer (the API's queued status).
                    first=lambda: (
                        self.run.status,
                        self.stored if self.stored is not None else self.run.progress_status,
                    ),
                )
            ),
            commit=AsyncMock(),
            rollback=AsyncMock(),
            flush=AsyncMock(),
        )
        self.ctx = SimpleNamespace(
            id="org-a",
            config=self.config,
            openai_api_key="k",
            jira=None,
            slack=None,
            github=SimpleNamespace(kind=kind),
        )
        self.registry = MagicMock()
        self.registry.get.return_value = self.ctx
        self.registry.resolve.return_value = RouteResult("org-a", "exact")
        self.registry.legacy_default_alias.return_value = None
        self.registry.is_disabled.return_value = False
        self.publisher = SimpleNamespace(publish=AsyncMock(return_value={}), last_comment_url=COMMENT_URL)
        if jira_issue is not None:
            self.jira = SimpleNamespace(
                enabled=lambda: True, project_allowed=lambda _key: True, get_issue=AsyncMock(return_value=jira_issue)
            )
        else:
            self.jira = SimpleNamespace(enabled=lambda: False, project_allowed=lambda _key: False)
        context = SimpleNamespace(
            skip_reason=None,
            files_total=1,
            files_reviewed=1,
            diff_lines=1,
            truncated=False,
            skipped_paths=[],
            prompt_version="t",
            prompt="p",
            changed_paths={"src/app.py"},
        )
        self.codex = AsyncMock(side_effect=codex_error) if codex_error else AsyncMock(return_value="{}")
        target = verified or _verified([])
        monkeypatch.setattr(orchestrator, "build_context", lambda **_kw: context)
        monkeypatch.setattr(orchestrator, "clone_head", AsyncMock(return_value=tmp_path))
        monkeypatch.setattr(orchestrator, "cleanup_checkout", lambda _path: None)
        monkeypatch.setattr(orchestrator, "parse_json_payload", lambda _raw: {})
        monkeypatch.setattr(orchestrator, "verify_review", lambda *_a, **_kw: target)
        monkeypatch.setattr(orchestrator, "_previous_head", AsyncMock(return_value=None))
        monkeypatch.setattr(orchestrator, "_persist_snapshot", AsyncMock())
        monkeypatch.setattr(orchestrator, "_store_findings", AsyncMock(return_value=[]))
        monkeypatch.setattr(orchestrator, "_private_publication_status", AsyncMock(return_value="private"))
        monkeypatch.setattr(orchestrator, "_check_public_alignment", AsyncMock(return_value="unknown"))

    async def queue(self) -> None:
        reporter = ps.ProgressReporter.build(self.github, self.config, self.ctx.github.kind)
        assert reporter is not None
        await reporter.queued(self.run, "org/repo")

    async def review(self):
        return await run_review(
            self.session,
            self.run.id,
            settings=SimpleNamespace(openai_api_key=""),
            config=self.config,
            github=self.github,
            jira=self.jira,
            publisher=self.publisher,
            codex_fn=self.codex,
            registry=self.registry,
        )


# --- GitHub App: check run lifecycle ---


@pytest.mark.asyncio
async def test_app_check_run_goes_queued_in_progress_completed_and_links_comment(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path, verified=_verified([_finding("P1")]))
    await h.queue()
    result = await h.review()

    assert result.status == "completed"
    assert h.github.check_states() == ["queued", "in_progress", "completed"]
    created = h.github.payloads("create")[0]
    assert created["name"] == "autoreview" and created["head_sha"] == HEAD
    assert created["external_id"] == str(h.run.id)
    updates = [call for call in h.github.calls if call[0] == "update"]
    assert all(call[1] == 101 for call in updates)
    final = updates[-1][2]
    assert final["conclusion"] == "neutral"
    assert final["details_url"] == COMMENT_URL
    assert COMMENT_URL in final["output"]["summary"]
    assert "P1=1" in final["output"]["summary"]
    assert h.run.progress_status["state"] == "completed"
    assert h.run.progress_status["check_run_id"] == 101
    assert h.github.payloads("status") == []


@pytest.mark.asyncio
async def test_worker_creates_check_run_when_queued_status_is_missing(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path)
    await h.review()
    assert [call[0] for call in h.github.calls] == ["create", "update"]
    assert h.github.payloads("create")[0]["status"] == "in_progress"
    assert h.github.calls[-1][2]["conclusion"] == "success"


# --- PAT: commit status ---


@pytest.mark.asyncio
async def test_pat_commit_status_pending_then_final_with_description_and_target_url(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path, kind="pat", verified=_verified([_finding("P3")]))
    await h.queue()
    await h.review()

    statuses = h.github.payloads("status")
    assert [item["state"] for item in statuses] == ["pending", "pending", "success"]
    assert all(item["context"] == "autoreview" for item in statuses)
    assert all(call[1] == HEAD for call in h.github.calls if call[0] == "status")
    final = statuses[-1]
    assert final["target_url"] == COMMENT_URL
    assert final["description"].startswith("Review published. Findings: P0=0 P1=0 P2=0 P3=1.")
    assert len(final["description"]) <= 140
    assert h.github.payloads("create") == [] and h.github.payloads("update") == []


@pytest.mark.parametrize(
    ("mode", "kind", "expected"),
    [
        ("auto", "app", "check_run"),
        ("auto", "pat", "commit_status"),
        ("check_run", "pat", "check_run"),
        ("commit_status", "app", "commit_status"),
        ("off", "app", None),
        ("auto", None, None),
    ],
)
def test_resolve_mode(mode, kind, expected) -> None:
    config = AppConfig(progress_status=ProgressStatusYaml(mode=mode))
    assert ps.resolve_mode(config, kind) == expected


def test_disabled_config_turns_reporting_off() -> None:
    config = AppConfig(progress_status=ProgressStatusYaml(enabled=False))
    assert ps.resolve_mode(config, "app") is None


def test_existing_configs_default_to_enabled_auto_without_reaction() -> None:
    config = AppConfig.model_validate({"features": {"jira_comment": False}})
    assert config.progress_status == ProgressStatusYaml(enabled=True, mode="auto", reaction=False)


@pytest.mark.asyncio
async def test_mode_off_makes_no_status_calls(monkeypatch, tmp_path) -> None:
    config = AppConfig(progress_status=ProgressStatusYaml(mode="off"))
    h = Harness(monkeypatch, tmp_path, config=config)
    result = await h.review()
    assert result.status == "completed"
    assert h.github.calls == []
    assert h.run.progress_status is None


# --- Skip reasons ---


@pytest.mark.asyncio
async def test_worker_draft_skip_shows_reason(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path, draft=True)
    await h.queue()
    result = await h.review()
    assert result.status == "skipped"
    final = h.github.calls[-1][2]
    assert final["conclusion"] == "skipped"
    assert final["output"]["summary"] == "Skipped: pull request is a draft."


@pytest.mark.asyncio
async def test_worker_size_guard_skip_shows_reason(monkeypatch, tmp_path) -> None:
    metrics = {"commits": 151, "additions": 0, "deletions": 0, "changed_files": 1}
    h = Harness(monkeypatch, tmp_path, kind="pat", size_metrics=metrics)
    result = await h.review()
    assert result.status == "skipped"
    final = h.github.payloads("status")[-1]
    assert final["state"] == "success"
    assert final["description"] == "Skipped: pull request is too large for AI review (size guard)."
    assert final["target_url"] == COMMENT_URL
    h.codex.assert_not_awaited()


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _webhook_client():
    from app.api.v1.pull_request import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = MagicMock()
    app.state.redis = MagicMock()
    return TestClient(app)


def _webhook_payload(*, draft: bool = False, fork: bool = False) -> dict:
    head_repo = {"full_name": "someone/repo" if fork else "org/repo", "fork": fork}
    return {
        "action": "opened",
        "repository": {"full_name": "org/repo", "private": True},
        "pull_request": {
            "number": 7,
            "draft": draft,
            "head": {"sha": HEAD, "ref": "feature", "repo": head_repo},
            "base": {"sha": "b" * 40, "repo": {"full_name": "org/repo"}},
        },
    }


@pytest.mark.parametrize(
    ("payload_kwargs", "route", "reason", "text"),
    [
        ({"draft": True}, RouteResult("org-a", "explicit"), SKIP_DRAFT, "pull request is a draft"),
        ({"fork": True}, RouteResult("org-a", "explicit"), SKIP_FORK, "pull requests from forks are not reviewed"),
        ({}, RouteResult("", REJECT_REPO_NOT_ALLOWED), SKIP_REPO, "repository is not allowed"),
    ],
)
def test_webhook_skip_reports_reason_with_owner_credentials(payload_kwargs, route, reason, text) -> None:
    secret = "owner-webhook-secret-123"
    ctx = SimpleNamespace(
        id="org-a",
        config=AppConfig(),
        github=SimpleNamespace(kind="app"),
        webhook_secret=secret,
        webhook_enabled=lambda: True,
    )
    registry = MagicMock()
    registry.get.return_value = ctx
    registry.canonicalize.return_value = "org-a"
    registry.resolve.return_value = route
    fake = FakeGitHub()
    body = json.dumps(_webhook_payload(**payload_kwargs)).encode()
    with (
        patch("app.api.v1.pull_request.get_owner_registry", return_value=registry),
        patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=fake) as build,
        patch("app.api.v1.pull_request.queue_review", AsyncMock()) as queue,
    ):
        response = _webhook_client().post(
            "/pull-request/org-a",
            content=body,
            headers={"X-Hub-Signature-256": _sign(secret, body), "X-GitHub-Event": "pull_request"},
        )
    assert response.status_code == 202
    assert response.json() == {"status": "skipped", "reason": reason}
    queue.assert_not_awaited()
    assert build.call_args.args[0] is ctx.github
    created = fake.payloads("create")
    assert len(created) == 1
    assert created[0]["status"] == "completed" and created[0]["conclusion"] == "skipped"
    assert created[0]["head_sha"] == HEAD
    assert created[0]["output"]["summary"] == f"Skipped: {text}."


@pytest.mark.parametrize(
    ("payload_kwargs", "route", "reason"),
    [
        ({"draft": True}, RouteResult("org-a", "exact"), SKIP_DRAFT),
        ({"fork": True}, RouteResult("org-a", "exact"), SKIP_FORK),
        ({}, RouteResult("", REJECT_REPO_NOT_ALLOWED), SKIP_REPO),
    ],
)
def test_root_webhook_skip_reports_with_signing_owner(payload_kwargs, route, reason) -> None:
    secret = "owner-webhook-secret-123"
    ctx = SimpleNamespace(id="org-a", config=AppConfig(), github=SimpleNamespace(kind="pat"))
    registry = MagicMock()
    registry.get.return_value = ctx
    registry.resolve.return_value = route
    fake = FakeGitHub()
    body = json.dumps(_webhook_payload(**payload_kwargs)).encode()
    with (
        patch("app.api.v1.pull_request.is_webhook_enabled", return_value=True),
        patch("app.api.v1.pull_request.get_owner_webhook_secrets", return_value={"org-a": secret}),
        patch("app.api.v1.pull_request.get_owner_registry", return_value=registry),
        patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=fake),
    ):
        response = _webhook_client().post(
            "/pull-request",
            content=body,
            headers={"X-Hub-Signature-256": _sign(secret, body), "X-GitHub-Event": "pull_request"},
        )
    assert response.json() == {"status": "skipped", "reason": reason}
    registry.get.assert_called_with("org-a")
    statuses = fake.payloads("status")
    assert len(statuses) == 1
    assert statuses[0]["state"] == "success" and statuses[0]["description"].startswith("Skipped: ")


def test_root_webhook_repo_not_allowed_without_single_signer_reports_nothing() -> None:
    secret = "shared-webhook-secret-123"
    registry = MagicMock()
    registry.resolve.return_value = RouteResult("", REJECT_REPO_NOT_ALLOWED)
    fake = FakeGitHub()
    body = json.dumps(_webhook_payload()).encode()
    with (
        patch("app.api.v1.pull_request.is_webhook_enabled", return_value=True),
        patch("app.api.v1.pull_request.get_owner_webhook_secrets", return_value={"org-a": secret, "org-b": secret}),
        patch("app.api.v1.pull_request.get_owner_registry", return_value=registry),
        patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=fake),
    ):
        response = _webhook_client().post(
            "/pull-request",
            content=body,
            headers={"X-Hub-Signature-256": _sign(secret, body), "X-GitHub-Event": "pull_request"},
        )
    assert response.json() == {"status": "skipped", "reason": SKIP_REPO}
    assert fake.calls == []
    registry.get.assert_not_called()


def test_webhook_queues_with_owner_reporter() -> None:
    secret = "owner-webhook-secret-123"
    ctx = SimpleNamespace(
        id="org-a",
        config=AppConfig(),
        github=SimpleNamespace(kind="pat"),
        webhook_secret=secret,
        webhook_enabled=lambda: True,
    )
    registry = MagicMock()
    registry.get.return_value = ctx
    registry.canonicalize.return_value = "org-a"
    registry.resolve.return_value = RouteResult("org-a", "explicit")
    fake = FakeGitHub()
    body = json.dumps(_webhook_payload()).encode()
    run = SimpleNamespace(id=uuid.uuid4(), status="pending")
    with (
        patch("app.api.v1.pull_request.get_owner_registry", return_value=registry),
        patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=fake),
        patch("app.api.v1.pull_request.queue_review", AsyncMock(return_value=run)) as queue,
    ):
        response = _webhook_client().post(
            "/pull-request/org-a",
            content=body,
            headers={"X-Hub-Signature-256": _sign(secret, body), "X-GitHub-Event": "pull_request"},
        )
    assert response.status_code == 202
    reporter = queue.await_args.kwargs["progress"]
    assert reporter.mode == "commit_status" and reporter.github is fake


# --- Findings never fail the check ---


@pytest.mark.parametrize(
    ("severities", "expected"),
    [((), "success"), (("P3",), "success"), (("P2",), "neutral"), (("P1", "P3"), "neutral"), (("P0",), "neutral")],
)
def test_findings_produce_success_or_neutral_never_failure(severities, expected) -> None:
    counts = {level: severities.count(level) for level in ("P0", "P1", "P2", "P3")}
    outcome = ps.final_outcome(
        status="completed",
        error_code=None,
        skip_reason=None,
        summary={"finding_counts": counts},
        comment_url=COMMENT_URL,
        issue_key=None,
        visibility="private",
        config=AppConfig(),
    )
    assert outcome.conclusion == expected
    assert ps._commit_state(outcome) == "success"


@pytest.mark.asyncio
async def test_blocking_findings_through_review_are_neutral(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path, verified=_verified([_finding("P0"), _finding("P1", security=True)]))
    await h.review()
    assert h.github.calls[-1][2]["conclusion"] == "neutral"


def test_other_final_states() -> None:
    def outcome(**kw):
        base = dict(error_code=None, skip_reason=None, summary=None, comment_url=None, issue_key=None)
        base.update(kw)
        return ps.final_outcome(visibility="public", config=AppConfig(), **base)

    assert outcome(status="cancelled").conclusion == "cancelled"
    assert outcome(status="failed", error_code=HEAD_CHANGED).conclusion == "cancelled"
    failed = outcome(status="failed", error_code="CODEX_QUOTA")
    assert failed.conclusion == "failure" and "CODEX_QUOTA" in failed.summary
    too_large = outcome(status="completed", error_code=SKIP_TOO_LARGE)
    assert too_large.conclusion == "skipped"
    assert ps._commit_state(outcome(status="cancelled")) == "error"
    assert ps._commit_state(failed) == "failure"


# --- Errors, timeouts and stale cleanup ---


@pytest.mark.asyncio
async def test_worker_error_finalizes_status_as_failure(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path, codex_error=RuntimeError("boom"))
    await h.queue()
    with pytest.raises(RuntimeError):
        await h.review()
    final = h.github.calls[-1]
    assert final[0] == "update"
    assert final[2]["conclusion"] == "failure"
    assert "internal error" in final[2]["output"]["summary"]
    assert "boom" not in final[2]["output"]["summary"]
    h.session.rollback.assert_awaited()
    stored = h.session.execute.await_args_list[-1].args[0]
    assert "progress_status" in str(stored)


@pytest.mark.asyncio
async def test_worker_timeout_finalizes_status_as_failure(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path, kind="pat", codex_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await h.review()
    final = h.github.payloads("status")[-1]
    assert final["state"] == "failure"
    assert final["description"] == "The review did not finish: the review timed out."


@pytest.mark.asyncio
async def test_worker_marks_run_failed_on_job_timeout(monkeypatch) -> None:
    from app.workers import settings as worker

    run = SimpleNamespace(status="running", error_code=None, trigger="webhook", duration_ms=None)
    session = MagicMock()
    session.get = AsyncMock(return_value=run)
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(worker, "run_review", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await worker.review_pull_request({"session_factory": lambda: session}, str(uuid.uuid4()))
    assert run.status == "failed"
    assert run.error_code == "REVIEW_TIMEOUT"


def _stale_run(state: dict | None, *, owner_id: str = "org-a"):
    repository = SimpleNamespace(full_name="org/repo")
    return SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=owner_id,
        head_sha=HEAD,
        progress_status=state,
        pull_request=SimpleNamespace(number=7, repository=repository),
    )


def test_is_stale_uses_job_timeout_plus_margin() -> None:
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    cutoff = ps.stale_cutoff(900, now)
    assert cutoff == now - timedelta(seconds=900 + ps.STALE_MARGIN_SECONDS)
    old = (cutoff - timedelta(seconds=1)).isoformat()
    fresh = (cutoff + timedelta(seconds=1)).isoformat()
    assert ps.is_stale({"state": "in_progress", "updated_at": old}, cutoff)
    assert ps.is_stale({"state": "queued", "updated_at": old}, cutoff)
    assert not ps.is_stale({"state": "in_progress", "updated_at": fresh}, cutoff)
    assert not ps.is_stale({"state": "completed", "updated_at": old}, cutoff)
    assert not ps.is_stale(None, cutoff)


@pytest.mark.asyncio
async def test_cleanup_marks_stale_statuses_timed_out() -> None:
    now = datetime.now(UTC)
    old = (now - timedelta(hours=2)).isoformat()
    stale = _stale_run({"mode": "check_run", "state": "in_progress", "check_run_id": 77, "updated_at": old})
    fresh = _stale_run({"mode": "check_run", "state": "in_progress", "check_run_id": 78, "updated_at": now.isoformat()})
    orphan = _stale_run({"mode": "check_run", "state": "queued", "updated_at": old}, owner_id="gone")
    result = MagicMock()
    result.scalars.return_value.all.return_value = [stale, fresh, orphan]
    session = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    fake = FakeGitHub()
    ctx = SimpleNamespace(id="org-a", config=AppConfig(), github=SimpleNamespace(kind="app"))
    registry = MagicMock()
    registry.get.side_effect = lambda owner: ctx if owner == "org-a" else None
    registry.is_disabled.return_value = False

    with patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=fake):
        cleaned = await ps.cleanup_stale(session, registry, 900, now=now)

    assert cleaned == 2
    assert [call[1] for call in fake.calls] == [77]
    assert fake.calls[0][2]["conclusion"] == "failure"
    assert fake.calls[0][2]["output"]["title"] == "Review timed out"
    assert stale.progress_status["state"] == "completed"
    assert fresh.progress_status["state"] == "in_progress"
    assert orphan.progress_status["state"] == "completed"
    session.commit.assert_awaited()


# --- New push cancels the previous SHA ---


@pytest.mark.asyncio
async def test_new_push_cancels_in_progress_run_for_previous_sha() -> None:
    fake = FakeGitHub()
    reporter = ps.ProgressReporter(fake, "check_run", AppConfig())
    old = SimpleNamespace(
        id=uuid.uuid4(),
        head_sha="c" * 40,
        progress_status={"mode": "check_run", "state": "in_progress", "check_run_id": 9},
    )
    await _report_cancelled(reporter, old, "org/repo", 7)
    assert fake.calls[0][1] == 9
    assert fake.calls[0][2]["conclusion"] == "cancelled"
    assert old.progress_status["conclusion"] == "cancelled"

    done = SimpleNamespace(id=uuid.uuid4(), head_sha="d" * 40, progress_status={"state": "completed"})
    await _report_cancelled(reporter, done, "org/repo", 7)
    await _report_cancelled(None, old, "org/repo", 7)
    assert len(fake.calls) == 1


# --- Status failures never affect the review ---


@pytest.mark.parametrize("failure", ["error", "hang"])
@pytest.mark.asyncio
async def test_status_failures_do_not_affect_review(monkeypatch, tmp_path, failure) -> None:
    config = AppConfig(progress_status=ProgressStatusYaml(timeout_seconds=0.05, reaction=True))
    github = FakeGitHub(info=_info(), fail=failure == "error", hang=failure == "hang")
    h = Harness(monkeypatch, tmp_path, config=config, github=github, verified=_verified([_finding("P2")]))
    await h.queue()
    result = await h.review()
    assert result.status == "completed"
    h.publisher.publish.assert_awaited_once()
    assert h.run.comment_url == COMMENT_URL
    # Every step was attempted (no check run id came back, so each one tries to create it) and swallowed
    assert h.github.check_states() == ["queued", "in_progress", "completed"]
    assert [call[0] for call in h.github.calls].count("react") == 1


# --- Optional reaction ---


@pytest.mark.asyncio
async def test_reaction_added_on_pickup_and_removed_after_publishing(monkeypatch, tmp_path) -> None:
    config = AppConfig(progress_status=ProgressStatusYaml(reaction=True))
    h = Harness(monkeypatch, tmp_path, config=config)
    await h.review()
    kinds = [call[0] for call in h.github.calls]
    assert kinds == ["create", "react", "update", "unreact"]
    assert h.github.calls[1][1:] == (7, "eyes")
    assert h.github.calls[3][1:] == (7, 555)
    assert "reaction_id" not in h.run.progress_status


@pytest.mark.asyncio
async def test_no_reaction_by_default(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path)
    await h.review()
    assert not [call for call in h.github.calls if call[0] in {"react", "unreact"}]


# --- Disclosure ---


@pytest.mark.parametrize("kind", ["app", "pat"])
@pytest.mark.asyncio
async def test_public_repo_summary_has_no_jira_text_or_security_details(monkeypatch, tmp_path, kind) -> None:
    config = AppConfig(public_repos=PublicReposYaml(jira_disclosure="none"))
    verified = _verified([_finding("P1", security=True)], summary=SECRET_SUMMARY, unmet=["SECRET-CRITERIA must hold"])
    h = Harness(monkeypatch, tmp_path, kind=kind, visibility="public", config=config, verified=verified)
    await h.queue()
    await h.review()
    text = json.dumps(h.github.calls)
    for leaked in ("SECRET-CRITERIA", SECURITY_TITLE, "crafted username", "bound parameters", "ABC-12", "unclear"):
        assert leaked not in text
    assert "P1=1" in text


@pytest.mark.asyncio
async def test_public_repo_key_only_shows_key_but_private_details_stay_out(monkeypatch, tmp_path) -> None:
    verified = _verified([_finding("P2", security=True)], summary=SECRET_SUMMARY)
    h = Harness(monkeypatch, tmp_path, visibility="public", verified=verified)
    await h.review()
    summary = h.github.calls[-1][2]["output"]["summary"]
    assert "Task: ABC-12" in summary and "alignment" not in summary
    assert SECURITY_TITLE not in summary and "SECRET-CRITERIA" not in summary


@pytest.mark.asyncio
async def test_private_repo_summary_adds_task_and_alignment(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path, verified=_verified([_finding("P2", security=True)], summary=SECRET_SUMMARY))
    await h.review()
    summary = h.github.calls[-1][2]["output"]["summary"]
    assert "Task: ABC-12 (alignment: unclear)" in summary
    assert SECURITY_TITLE not in summary and "SECRET-CRITERIA" not in summary


# --- has_sensitive_content ---


def _transition(security: bool) -> FindingTransitionView:
    return FindingTransitionView(
        stable_id="t",
        status="resolved",
        path="src/app.py",
        line=1,
        title="XSS in template" if security else "Typo",
        category="security" if security else "style",
    )


def test_sensitive_content_rules() -> None:
    clean = _verified([_finding("P2")])
    assert not review_has_sensitive_content(clean, [], jira_in_context=False)
    assert review_has_sensitive_content(clean, [], jira_in_context=True)
    assert review_has_sensitive_content(_verified([_finding("P3", security=True)]), [], jira_in_context=False)
    assert review_has_sensitive_content(_verified([], unmet=["criterion"]), [], jira_in_context=False)
    assert review_has_sensitive_content(clean, [_transition(True)], jira_in_context=False)
    assert not review_has_sensitive_content(clean, [_transition(False)], jira_in_context=False)
    leaky = _verified([], summary="Possible SQL injection via the login form")
    assert review_has_sensitive_content(leaky, [], jira_in_context=False)


@pytest.mark.parametrize(
    ("visibility", "security", "jira", "expected"),
    [
        ("private", False, False, False),
        ("private", True, False, True),
        ("public", True, False, True),
        ("private", False, True, True),
        ("public", False, True, False),
    ],
)
@pytest.mark.asyncio
async def test_review_records_sensitive_flag_from_unredacted_comment(
    monkeypatch, tmp_path, visibility, security, jira, expected
) -> None:
    issue = None
    if jira:
        issue = SimpleNamespace(
            key="ABC-12",
            summary="secret summary",
            issue_type="Story",
            priority="High",
            status="In Progress",
            acceptance_criteria="secret criteria",
            description="secret description",
        )
    h = Harness(
        monkeypatch,
        tmp_path,
        visibility=visibility,
        verified=_verified([_finding("P2", security=security)]),
        jira_issue=issue,
    )
    await h.review()
    assert h.run.has_sensitive_content is expected
    assert h.run.comment_url == COMMENT_URL


@pytest.mark.asyncio
async def test_public_fallback_flag_reflects_unredacted_comment(monkeypatch, tmp_path) -> None:
    # The fallback strips the unmet criteria before publishing; the flag is about the unredacted text.
    h = Harness(monkeypatch, tmp_path, verified=_verified([], unmet=["SECRET-CRITERIA must hold"]))
    monkeypatch.setattr(orchestrator, "_private_publication_status", AsyncMock(return_value="public_fallback"))
    await h.review()
    published = h.publisher.publish.await_args.kwargs["render"].verified
    assert published.unmet_acceptance_criteria == []
    assert h.run.has_sensitive_content is True
    assert "Task: ABC-12 (alignment" not in h.github.calls[-1][2]["output"]["summary"]


@pytest.mark.asyncio
async def test_size_guard_comment_is_not_sensitive(monkeypatch, tmp_path) -> None:
    metrics = {"commits": 151, "additions": 0, "deletions": 0, "changed_files": 1}
    h = Harness(monkeypatch, tmp_path, size_metrics=metrics)
    await h.review()
    assert h.run.has_sensitive_content is False
    assert h.run.comment_url == COMMENT_URL


def test_stale_cleanup_cron_is_always_registered() -> None:
    from app.workers import settings as worker

    jobs = [job for job in worker._cron_jobs() if job.coroutine is worker.cleanup_progress_status]
    assert len(jobs) == 1
    assert jobs[0].minute == set(range(0, 60, 10))
    assert worker.cleanup_progress_status in worker.WorkerSettings.functions


def test_owner_can_override_progress_status() -> None:
    from app.owners.registry import OwnerConfigError
    from tests.test_owner_policies import policy_config, policy_registry

    config = policy_config()
    config.progress_status = ProgressStatusYaml(reaction=True)
    config.owners["org-a"]["progress_status"] = {"mode": "off"}
    registry = policy_registry(config)
    a, b = registry.get("org-a").config, registry.get("org-b").config
    assert (a.progress_status.mode, a.progress_status.reaction) == ("off", True)
    assert (b.progress_status.mode, b.progress_status.reaction) == ("auto", True)
    assert ps.resolve_mode(a, "pat") is None
    assert ps.resolve_mode(b, "pat") == "commit_status"

    config.owners["org-a"]["progress_status"] = {"timeout_seconds": 0}
    with pytest.raises(OwnerConfigError, match="progress_status"):
        policy_registry(config)


# --- Write order: the worker reuses a status the API stored after the worker loaded the run ---


@pytest.mark.asyncio
async def test_worker_reads_stored_state_instead_of_creating_a_second_check_run(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path)
    # The API stored its queued check run after this worker loaded the row (in-memory state is None).
    h.stored = {"mode": "check_run", "state": "queued", "check_run_id": 4242}
    await h.review()
    assert h.github.payloads("create") == []
    assert {call[1] for call in h.github.calls if call[0] == "update"} == {4242}
    assert h.github.check_states() == ["in_progress", "completed"]
    assert h.run.progress_status["check_run_id"] == 4242


# --- Disabled / removed owners in the worker ---


def _owner_gone(h: Harness, *, disabled: bool) -> None:
    h.registry.get.return_value = None
    h.registry.is_disabled.return_value = disabled
    h.registry.disabled_status_context.return_value = h.ctx if disabled else None


@pytest.mark.parametrize("kind", ["app", "pat"])
@pytest.mark.asyncio
async def test_worker_closes_disabled_owner_status_as_skipped(monkeypatch, tmp_path, kind) -> None:
    h = Harness(monkeypatch, tmp_path, kind=kind)
    await h.queue()
    _owner_gone(h, disabled=True)
    result = await h.review()

    assert result.status == "skipped" and result.skip_reason == "owner_disabled"
    h.registry.disabled_status_context.assert_called_with("org-a")
    if kind == "app":
        assert h.github.check_states() == ["queued", "completed"]
        final = h.github.calls[-1]
        assert final[1] == 101
        assert final[2]["conclusion"] == "skipped"
        assert "owner is disabled" in final[2]["output"]["summary"]
    else:
        states = [p["state"] for p in h.github.payloads("status")]
        assert states == ["pending", "success"]
        assert "owner is disabled" in h.github.payloads("status")[-1]["description"]
    assert h.run.progress_status["state"] == "completed"
    assert h.run.progress_status["conclusion"] == "skipped"


@pytest.mark.asyncio
async def test_worker_cannot_close_removed_owner_status(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path)
    await h.queue()
    calls_before = list(h.github.calls)
    _owner_gone(h, disabled=False)
    result = await h.review()

    assert result.status == "skipped" and result.skip_reason == "owner_not_configured"
    h.registry.disabled_status_context.assert_not_called()
    assert h.github.calls == calls_before  # no credentials of any other owner are used
    assert h.run.progress_status["state"] == "queued"  # left for the cleanup job


# --- Cleanup for disabled / removed owners ---


@pytest.mark.parametrize("kind", ["app", "pat"])
@pytest.mark.asyncio
async def test_cleanup_closes_disabled_owner_status_with_its_own_credentials(kind, caplog) -> None:
    now = datetime.now(UTC)
    old = (now - timedelta(hours=2)).isoformat()
    mode = "check_run" if kind == "app" else "commit_status"
    state = {"mode": mode, "state": "queued", "updated_at": old}
    if kind == "app":
        state["check_run_id"] = 77
    disabled = _stale_run(state, owner_id="org-x")
    removed = _stale_run({"mode": mode, "state": "in_progress", "updated_at": old}, owner_id="gone")
    result = MagicMock()
    result.scalars.return_value.all.return_value = [disabled, removed]
    session = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    fake = FakeGitHub()
    x_credentials = SimpleNamespace(kind=kind)
    ctx = SimpleNamespace(id="org-x", config=AppConfig(), github=x_credentials)
    registry = MagicMock()
    registry.get.return_value = None
    registry.is_disabled.side_effect = lambda owner: owner == "org-x"
    registry.disabled_status_context.side_effect = lambda owner: ctx if owner == "org-x" else None

    with patch("app.adapters.github.GitHubAppClient.from_credentials", return_value=fake) as build:
        cleaned = await ps.cleanup_stale(session, registry, 900, now=now)

    assert cleaned == 2
    build.assert_called_once_with(x_credentials, owner_id="org-x")
    if kind == "app":
        assert [call[:2] for call in fake.calls] == [("update", 77)]
        assert fake.calls[0][2]["conclusion"] == "skipped"
        assert "owner is disabled" in fake.calls[0][2]["output"]["summary"]
    else:
        assert [call[2]["state"] for call in fake.calls] == ["success"]
        assert "owner is disabled" in fake.calls[0][2]["description"]
    assert disabled.progress_status["conclusion"] == "skipped"
    assert removed.progress_status["state"] == "completed"
    assert removed.progress_status["conclusion"] == "failure"
    assert "cannot be closed on GitHub" in caplog.text and "gone" in caplog.text


def test_registry_keeps_disabled_owner_credentials_only_for_status_cleanup() -> None:
    from tests.test_owner_policies import policy_config, policy_registry

    config = policy_config()
    config.owners["org-b"]["enabled"] = False
    config.owners["org-c"]["enabled"] = False
    registry = policy_registry(config, env={k: v for k, v in _owner_secrets().items() if "ORG_C" not in k})

    assert registry.get("org-b") is None and registry.is_disabled("org-b")
    b = registry.disabled_status_context("org-b")
    assert b is not None and b.id == "org-b" and b.github.kind == "pat"
    assert b.github.token == "ghp_b"
    assert registry.disabled_status_context("org-c") is None  # no usable credentials: startup still works
    assert registry.disabled_status_context("org-a") is None  # enabled owners are never served here
    assert registry.disabled_status_context("unknown") is None


def _owner_secrets() -> dict[str, str]:
    from tests.test_owner_policies import SECRETS

    return dict(SECRETS)


@pytest.mark.asyncio
async def test_disabled_owner_skip_closes_the_stored_check_run(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path)
    h.stored = {"mode": "check_run", "state": "queued", "check_run_id": 4242}
    _owner_gone(h, disabled=True)
    await h.review()
    assert h.github.payloads("create") == []
    assert [call[:2] for call in h.github.calls] == [("update", 4242)]
    assert h.github.calls[0][2]["conclusion"] == "skipped"


def _queued_session(status: str, state: dict | None, *, commit_error: Exception | None = None):
    result = SimpleNamespace(first=lambda: (status, state))
    return SimpleNamespace(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(side_effect=commit_error),
        rollback=AsyncMock(),
    )


def _pending_run():
    return SimpleNamespace(id=uuid.uuid4(), head_sha=HEAD, progress_status=None)


@pytest.mark.parametrize(
    "status,state", [("running", None), ("completed", None), ("pending", {"state": "in_progress"})]
)
@pytest.mark.asyncio
async def test_queued_write_is_skipped_once_the_worker_owns_the_status(status, state) -> None:
    from app.services.review_enqueue import _report_queued

    fake = FakeGitHub()
    session = _queued_session(status, state)
    run = _pending_run()
    await _report_queued(session, ps.ProgressReporter(fake, "check_run", AppConfig()), run, "org/repo")
    assert fake.calls == []
    assert run.progress_status is None
    session.commit.assert_awaited()  # the row lock is released


@pytest.mark.asyncio
async def test_unrecorded_queued_check_run_is_closed() -> None:
    from app.services.review_enqueue import _report_queued

    fake = FakeGitHub()
    session = _queued_session("pending", None, commit_error=RuntimeError("db gone"))
    await _report_queued(session, ps.ProgressReporter(fake, "check_run", AppConfig()), _pending_run(), "org/repo")
    assert [call[0] for call in fake.calls] == ["create", "update"]
    assert fake.calls[0][1]["status"] == "queued"
    assert fake.calls[1][1] == 101
    assert fake.calls[1][2]["status"] == "completed" and fake.calls[1][2]["conclusion"] == "cancelled"
    session.rollback.assert_awaited()
