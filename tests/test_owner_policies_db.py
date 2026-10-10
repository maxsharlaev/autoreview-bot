"""Full run_review per owner against Postgres: policy overrides and the model call.

Uses the real OwnerRegistry from tests.test_owner_policies (mode B: legacy 'default', org-a with
most overrides, org-b with none, org-c with its own model), the real Publisher.from_context,
JiraClient and run_codex. GitHub reads come from a double; the sticky-comment upsert is recorded.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from app.adapters.github import GitHubAppClient
from app.services import codex_runner
from app.services.constants import SKIP_TOO_LARGE
from tests.test_cross_owner_db import BASE_SHA, HEAD_SHA, _mock_github, requires_test_postgres
from tests.test_owner_integrations import JiraSite, SlackApi
from tests.test_owner_integrations_db import session_factory  # noqa: F401  (fixture)
from tests.test_owner_policies import A_JIRA, B_JIRA, SECRETS, SHARED_MODEL_KEY, policy_registry, policy_settings

pytestmark = requires_test_postgres

SECRET_VALUES = set(SECRETS.values()) | {SHARED_MODEL_KEY, "ghp_legacy", "xoxb-legacy", "sk-process-env"}


def _review_payload(category: str) -> dict:
    return {
        "schema_version": 1,
        "reviewed_head_sha": HEAD_SHA,
        "previous_reviewed_head_sha": None,
        "summary": "Owner summary",
        "task_alignment": {"issue_key": None, "status": "unclear", "unmet_acceptance_criteria": []},
        "previous_findings": [],
        "findings": [
            {
                "id": "f1",
                "severity": "P1",
                "confidence": 0.9,
                "category": category,
                "path": "src/app.py",
                "line": 10,
                "title": "Loop bound is off by one",
                "scenario": "the last item is skipped",
                "evidence": "i < n - 1",
                "recommendation": "iterate to n",
                "blocking_candidate": True,
            }
        ],
        "coverage": {"files_total": 0, "files_reviewed": 0, "truncated": False, "skipped_paths": []},
    }


def _fake_codex(calls: list[dict], category: str = "correctness"):
    async def codex(*, output_path, **kwargs):
        calls.append(kwargs)
        raw = json.dumps(_review_payload(category))
        output_path.write_text(raw, encoding="utf-8")
        return raw

    return codex


async def _full_review(
    session_factory,  # noqa: F811
    tmp_path,
    *,
    owner_id: str,
    full_name: str,
    issue_key: str = "ZZZ-1",
    visibility: str = "private",
    commits: int = 1,
    codex_fn=None,
):
    from app.services import orchestrator
    from app.services.review_enqueue import queue_review

    registry = policy_registry()
    redis = MagicMock()
    redis.enqueue_job = AsyncMock(return_value=MagicMock(job_id="job"))
    async with session_factory() as session:
        run = await queue_review(
            session,
            redis,
            full_name=full_name,
            number=7,
            pr_payload={
                "number": 7,
                "html_url": f"https://github.com/{full_name}/pull/7",
                "title": f"{issue_key} change",
                "user": {"login": "author"},
                "state": "open",
                "head": {"sha": HEAD_SHA, "ref": "feature"},
                "base": {"sha": BASE_SHA},
            },
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            is_fork=False,
            repository_visibility=visibility,
            owner_id=owner_id,
        )
        run_id = run.id

    github = _mock_github(full_name, 7)
    info = github.get_pull_request.return_value
    github.get_pull_request = AsyncMock(
        return_value=replace(info, title=f"{issue_key} change", visibility=visibility, commits_count=commits)
    )
    checkout = tmp_path / str(run_id)
    checkout.mkdir()
    codex_calls: list[dict] = []

    async def fake_clone(**_kwargs):
        return checkout

    async with session_factory() as session:
        with (
            patch.object(orchestrator, "clone_head", fake_clone),
            patch.object(orchestrator, "cleanup_checkout", lambda _path: None),
            patch.object(GitHubAppClient, "upsert_sticky_comment", AsyncMock()) as sticky,
        ):
            run = await orchestrator.run_review(
                session,
                run_id,
                settings=policy_settings(),
                github=github,
                codex_fn=codex_fn or _fake_codex(codex_calls),
                registry=registry,
            )
    bodies = [call.args[3] for call in sticky.await_args_list]
    return SimpleNamespace(run=run, bodies=bodies, github=github, codex_calls=codex_calls)


@pytest.fixture
def http():
    with respx.mock(assert_all_called=False) as router:
        yield SimpleNamespace(a=JiraSite(router, A_JIRA), b=JiraSite(router, B_JIRA), slack=SlackApi(router))


# --- public_repos disclosure and language per owner ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_id", "full_name", "details_shown", "redacted_label"),
    [
        ("org-a", "org-a/app", False, "Возможная проблема безопасности."),  # redact_all
        ("org-b", "org-b/app", True, None),  # global redact: non-security details stay
        ("default", "legacy-org/app", True, None),
    ],
)
async def test_public_repo_disclosure_per_owner(
    session_factory,  # noqa: F811
    tmp_path,
    http,
    owner_id,
    full_name,
    details_shown,
    redacted_label,
):
    result = await _full_review(session_factory, tmp_path, owner_id=owner_id, full_name=full_name, visibility="public")

    assert result.run.status == "completed", result.run.error_code
    [body] = result.bodies
    for detail in ("Loop bound is off by one", "the last item is skipped", "i < n - 1", "iterate to n"):
        assert (detail in body) is details_shown, detail
    if redacted_label:
        assert redacted_label in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_id", "full_name", "label", "foreign_label"),
    [
        ("org-a", "org-a/app", "Рекомендация", "Recommendation"),
        ("org-b", "org-b/app", "Recommendation", "Рекомендация"),
    ],
)
async def test_comment_language_per_owner(
    session_factory,  # noqa: F811
    tmp_path,
    http,
    owner_id,
    full_name,
    label,
    foreign_label,
):
    result = await _full_review(session_factory, tmp_path, owner_id=owner_id, full_name=full_name)

    [body] = result.bodies
    assert "Loop bound is off by one" in body
    assert label in body and foreign_label not in body


# --- size guard per owner ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_id", "full_name", "status"),
    [("org-a", "org-a/app", "skipped"), ("org-b", "org-b/app", "completed")],
)
async def test_size_guard_per_owner_in_run_review(session_factory, tmp_path, http, owner_id, full_name, status):  # noqa: F811
    result = await _full_review(session_factory, tmp_path, owner_id=owner_id, full_name=full_name, commits=4)

    assert result.run.status == status
    if status == "skipped":
        assert result.run.skip_reason == SKIP_TOO_LARGE
        assert result.codex_calls == []
        [call] = result.github.upsert_sticky_comment.await_args_list
        assert "`a:force`" in call.args[3]
    else:
        assert len(result.codex_calls) == 1


# --- features per owner ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_features_off_no_jira_comment_or_transition_in_run_review(
    session_factory,  # noqa: F811
    tmp_path,
    http,
):
    result = await _full_review(session_factory, tmp_path, owner_id="org-a", full_name="org-a/app", issue_key="AAA-7")

    assert result.run.status == "completed", result.run.error_code
    assert [(m, p) for m, p, _ in http.a.calls] == [("GET", "/rest/api/3/issue/AAA-7")]
    assert http.b.calls == []


@pytest.mark.asyncio
async def test_owner_features_on_comment_and_transition_in_run_review(session_factory, tmp_path, http):  # noqa: F811
    await _full_review(session_factory, tmp_path, owner_id="org-b", full_name="org-b/app", issue_key="BBB-7")

    assert [m for m, _p, _ in http.b.calls] == ["GET", "POST", "GET", "POST"]
    assert http.a.calls == []


# --- model key, model and reasoning effort reach the Codex subprocess ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_id", "full_name", "key", "model", "effort"),
    [
        ("org-a", "org-a/app", "sk-org-a", "model-a", "high"),
        ("org-c", "org-c/app", "sk-org-c", "model-c", "medium"),
        ("org-b", "org-b/app", SHARED_MODEL_KEY, "global-model", "medium"),
        ("default", "legacy-org/app", SHARED_MODEL_KEY, "global-model", "medium"),
    ],
)
async def test_codex_subprocess_gets_only_run_owner_model_key(
    session_factory,  # noqa: F811
    tmp_path,
    http,
    monkeypatch,
    owner_id,
    full_name,
    key,
    model,
    effort,
):
    # Every owner secret is also in the process environment; only the run owner's key may pass.
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-process-env")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_legacy")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-legacy")

    launches: list[tuple[list[str], dict[str, str]]] = []
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*cmd, cwd, env, **kwargs):
        launches.append((list(cmd), dict(env)))
        output = cmd[cmd.index("-o") + 1]
        with open(output, "w", encoding="utf-8") as fh:
            json.dump(_review_payload("correctness"), fh)
        return await real_exec(sys.executable, "-c", "import sys; sys.stdin.read()", cwd=cwd, env=env, **kwargs)

    with (
        patch.object(codex_runner.shutil, "which", lambda _name: "/opt/fake/codex"),
        patch.object(codex_runner.asyncio, "create_subprocess_exec", fake_exec),
    ):
        result = await _full_review(
            session_factory, tmp_path, owner_id=owner_id, full_name=full_name, codex_fn=codex_runner.run_codex
        )

    assert result.run.status == "completed", result.run.error_code
    # org-a also has pr_description enabled: the description call uses the same owner settings.
    assert len(launches) == (2 if owner_id == "org-a" else 1)
    for cmd, env in launches:
        assert env["OPENAI_API_KEY"] == env["CODEX_API_KEY"] == key
        assert set(env.values()) & SECRET_VALUES == {key}
        assert not [name for name in env if name.startswith("OWNER_") or name.endswith("_MODEL_KEY")]
        assert cmd[cmd.index("-m") + 1] == model
        assert f'model_reasoning_effort="{effort}"' in cmd
    assert result.run.model_version == model
