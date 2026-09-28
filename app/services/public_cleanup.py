"""Redact bot-owned GitHub text when a repository becomes public."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.github import GitHubAppClient, GitHubError
from app.models import PullRequest, Repository
from app.services.constants import REVIEW_MARKER
from app.services.pr_description import (
    COMMENT_MARKER,
    render_pr_description_comment,
    without_managed_block,
)

logger = logging.getLogger(__name__)

_WITHHELD = "Previously published review details were withheld after repository visibility changed."
_DESCRIPTION_WITHHELD = "Previously generated description withheld after repository visibility changed."


async def redact_public_repository(session: AsyncSession, github: GitHubAppClient, full_name: str) -> int:
    repository = (
        await session.execute(select(Repository).where(Repository.full_name == full_name))
    ).scalar_one_or_none()
    if repository is None:
        return 0
    owner, repo = full_name.split("/", 1)
    github.previous_comment_authors = tuple(repository.comment_authors or ())
    pull_requests = (
        (await session.execute(select(PullRequest).where(PullRequest.repository_id == repository.id))).scalars().all()
    )
    updated = 0
    failures = 0
    for pr in pull_requests:
        try:
            current = await github.get_pull_request(owner, repo, pr.number)
            if current.visibility == "private":
                continue
            updated += await github.redact_managed_comments(
                owner,
                repo,
                pr.number,
                {
                    REVIEW_MARKER: f"{REVIEW_MARKER}\n\n{_WITHHELD}",
                    COMMENT_MARKER: render_pr_description_comment(_DESCRIPTION_WITHHELD),
                },
            )
            clean_body = without_managed_block(current.body)
            if clean_body != current.body:
                await github.update_pull_request_body(owner, repo, pr.number, clean_body)
                updated += 1
            if pr.bot_title and current.title == pr.bot_title:
                safe_title = "chore: review changes"
                await github.update_pull_request_title(owner, repo, pr.number, safe_title)
                pr.bot_title = safe_title
                pr.title = safe_title
                updated += 1
        except GitHubError:
            failures += 1
            logger.exception("Could not redact managed content for %s#%s", full_name, pr.number)
    await session.commit()
    if failures:
        raise GitHubError(f"Could not redact {failures} pull requests in {full_name}")
    repository.last_visibility = "public"
    await session.commit()
    return updated


async def reconcile_repository_visibility(session: AsyncSession, github: GitHubAppClient) -> int:
    """Catch missed public events for repositories already tracked by the service."""
    repositories = (await session.execute(select(Repository))).scalars().all()
    updated = 0
    for repository in repositories:
        owner, repo = repository.full_name.split("/", 1)
        try:
            visibility = await github.get_repository_visibility(owner, repo)
            if visibility == "public" and repository.last_visibility != "public":
                updated += await redact_public_repository(session, github, repository.full_name)
            elif visibility == "private" and repository.last_visibility != "private":
                repository.last_visibility = "private"
                await session.commit()
        except GitHubError:
            logger.exception("Could not reconcile repository visibility for %s", repository.full_name)
    return updated
