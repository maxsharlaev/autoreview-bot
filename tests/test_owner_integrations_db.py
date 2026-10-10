"""Full run_review per owner against Postgres: Jira and Slack traffic goes only to the owner's bindings.

Uses the real OwnerRegistry from tests.test_owner_integrations (mode B with a legacy Jira site
and Slack channel), the real Publisher.from_context and JiraClient, and respx for Jira/Slack.
GitHub reads come from a double; the sticky-comment upsert is stubbed.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
from app.adapters.github import GitHubAppClient
from tests.test_cross_owner_db import (
    BASE_SHA,
    HEAD_SHA,
    _clear_findings,
    _downgrade_migrations,
    _get_alembic_config,
    _get_asyncpg_url,
    _mock_github,
    _recording_codex,
    _run_migrations,
    requires_test_postgres,
)
from tests.test_owner_integrations import (
    B_AUTH,
    B_JIRA,
    D_AUTH,
    D_JIRA,
    LEGACY_AUTH,
    LEGACY_JIRA,
    JiraSite,
    SlackApi,
    build_registry,
    global_legacy_integrations,  # noqa: F401  (autouse: global settings carry the legacy Jira/Slack)
    legacy_settings,
)

pytestmark = requires_test_postgres


@pytest.fixture
async def session_factory():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    config = _get_alembic_config()
    await _clear_findings()
    _downgrade_migrations(config, "base")
    _run_migrations(config, "head")
    engine = create_async_engine(_get_asyncpg_url())
    yield sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()
    await _clear_findings()
    _downgrade_migrations(config, "base")


async def _full_review(session_factory, tmp_path, *, owner_id: str, full_name: str, issue_key: str):
    """Queue and run one review for owner_id with no client overrides except GitHub reads."""
    from app.models import TaskSnapshot
    from app.services import orchestrator
    from app.services.review_enqueue import queue_review
    from sqlalchemy import select

    registry = build_registry()
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
            owner_id=owner_id,
        )
        run_id = run.id

    github = _mock_github(full_name, 7)
    info = github.get_pull_request.return_value
    github.get_pull_request = AsyncMock(return_value=replace(info, title=f"{issue_key} change"))
    checkout = tmp_path / str(run_id)
    checkout.mkdir()
    prompts: list[str] = []

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
                settings=legacy_settings(),
                github=github,
                codex_fn=_recording_codex(prompts, stable_id="f1", tag=owner_id.upper()),
                registry=registry,
            )
        snapshot = (
            await session.execute(select(TaskSnapshot).where(TaskSnapshot.review_run_id == run_id))
        ).scalar_one()
    assert run.status == "completed", run.error_code
    assert sticky.await_count == 1
    return prompts, snapshot


@pytest.fixture
def http():
    from types import SimpleNamespace

    with respx.mock(assert_all_called=False) as router:
        yield SimpleNamespace(
            legacy=JiraSite(router, LEGACY_JIRA),
            b=JiraSite(router, B_JIRA),
            d=JiraSite(router, D_JIRA),
            slack=SlackApi(router),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("issue_key", ["LEG-7", "BBB-7"])
async def test_owner_without_jira_or_slack_makes_no_integration_calls(session_factory, tmp_path, http, issue_key):
    prompts, snapshot = await _full_review(
        session_factory, tmp_path, owner_id="org-c", full_name="org-c/app", issue_key=issue_key
    )

    assert http.legacy.route.call_count == http.b.route.call_count == http.d.route.call_count == 0
    assert http.slack.route.call_count == 0
    assert f"{issue_key} summary" not in prompts[0]
    assert (snapshot.issue_key, snapshot.jira_status, snapshot.comment_posted, snapshot.transition_done) == (
        issue_key,
        None,
        False,
        False,
    )


@pytest.mark.asyncio
async def test_owner_reads_comments_and_transitions_on_own_site(session_factory, tmp_path, http):
    prompts, snapshot = await _full_review(
        session_factory, tmp_path, owner_id="org-b", full_name="org-b/app", issue_key="BBB-7"
    )

    assert [(m, p) for m, p, _ in http.b.calls] == [
        ("GET", "/rest/api/3/issue/BBB-7"),
        ("POST", "/rest/api/2/issue/BBB-7/comment"),
        ("GET", "/rest/api/3/issue/BBB-7/transitions"),
        ("POST", "/rest/api/3/issue/BBB-7/transitions"),
    ]
    assert http.b.authorizations == {B_AUTH}
    assert http.b.transition_targets == ["Fix B"]
    assert http.slack.posts == [("Bearer xoxb-b", "#org-b")]
    assert http.legacy.calls == [] and http.d.calls == []
    assert "BBB-7 summary" in prompts[0]
    assert (snapshot.jira_status, snapshot.comment_posted, snapshot.transition_done) == ("In Review", True, True)


@pytest.mark.asyncio
async def test_owner_does_not_touch_projects_outside_its_jira(session_factory, tmp_path, http):
    prompts, snapshot = await _full_review(
        session_factory, tmp_path, owner_id="org-b", full_name="org-b/app", issue_key="LEG-7"
    )

    assert http.legacy.calls == [] and http.b.calls == [] and http.d.calls == []
    assert http.slack.posts == [("Bearer xoxb-b", "#org-b")]
    assert "LEG-7 summary" not in prompts[0]
    assert (snapshot.comment_posted, snapshot.transition_done) == (False, False)


@pytest.mark.asyncio
async def test_second_owner_uses_own_site_and_rework_status(session_factory, tmp_path, http):
    _prompts, snapshot = await _full_review(
        session_factory, tmp_path, owner_id="org-d", full_name="org-d/app", issue_key="DDD-7"
    )

    assert http.d.authorizations == {D_AUTH}
    assert http.d.transition_targets == ["Rework D"]
    assert http.legacy.calls == [] and http.b.calls == []
    assert http.slack.route.call_count == 0
    assert (snapshot.comment_posted, snapshot.transition_done) == (True, True)


@pytest.mark.asyncio
async def test_legacy_owner_unchanged(session_factory, tmp_path, http):
    _prompts, snapshot = await _full_review(
        session_factory, tmp_path, owner_id="default", full_name="legacy-org/app", issue_key="LEG-7"
    )

    assert http.legacy.authorizations == {LEGACY_AUTH}
    assert http.legacy.transition_targets == ["Legacy Rework"]
    assert http.slack.posts == [("Bearer xoxb-legacy", "#legacy")]
    assert http.b.calls == [] and http.d.calls == []
    assert (snapshot.comment_posted, snapshot.transition_done) == (True, True)
