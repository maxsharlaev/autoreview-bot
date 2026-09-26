from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.github import GitHubAppClient
from app.adapters.slack import SlackClient, SlackError
from app.config import AppConfig, get_app_config
from app.models import DigestRun, PullRequest, Repository

logger = logging.getLogger(__name__)


def _age_hours(created_at: datetime | None) -> float:
    if created_at is None:
        return 0.0
    now = datetime.now(UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return max(0.0, (now - created_at).total_seconds() / 3600)


def format_digest_text(payload: dict[str, Any]) -> str:
    lines = [
        "*AI Review digest*",
        f"Open PRs: {payload.get('open_pr_count', 0)}",
        f"With P0/P1: {payload.get('blocker_pr_count', 0)}",
        "",
    ]
    items = payload.get("items") or []
    if not items:
        lines.append("No open pull requests in the review store.")
        return "\n".join(lines)
    for item in items:
        blockers = "blockers" if item.get("has_blockers") else "clean/pending"
        assignee = item.get("assignee") or "unassigned"
        lines.append(
            f"• {item.get('repository')}#{item.get('number')} "
            f"({blockers}, {assignee}, {item.get('age_hours', 0):.1f}h) "
            f"{item.get('html_url') or ''}"
        )
    return "\n".join(lines)


async def build_digest_payload(session: AsyncSession) -> dict[str, Any]:
    result = await session.execute(
        select(PullRequest)
        .options(selectinload(PullRequest.repository), selectinload(PullRequest.findings))
        .where(PullRequest.state == "open")
        .order_by(PullRequest.updated_at.desc())
    )
    items = []
    blocker_count = 0
    for pr in result.scalars().unique():
        has_blockers = any(
            finding.current_status == "open" and finding.severity in {"P0", "P1"} for finding in pr.findings
        )
        if has_blockers:
            blocker_count += 1
        items.append(
            {
                "repository": pr.repository.full_name if pr.repository else "",
                "number": pr.number,
                "html_url": pr.html_url,
                "assignee": pr.assignee or pr.author,
                "age_hours": round(_age_hours(pr.created_at), 1),
                "has_blockers": has_blockers,
                "head_sha": pr.head_sha,
            }
        )
    return {
        "open_pr_count": len(items),
        "blocker_pr_count": blocker_count,
        "items": items,
        "generated_at": datetime.now(UTC).isoformat(),
    }


async def run_digest(
    session: AsyncSession,
    *,
    config: AppConfig | None = None,
    slack: SlackClient | None = None,
    github: GitHubAppClient | None = None,
) -> DigestRun:
    cfg = config or get_app_config()
    payload = await build_digest_payload(session)
    if github and cfg.github.allowed_repos:
        # Best-effort refresh of open PR metadata for registered repos.
        for full_name in cfg.github.allowed_repos:
            if "/" not in full_name:
                continue
            owner, repo = full_name.split("/", 1)
            try:
                pulls = await github.list_open_pulls(owner, repo)
            except Exception:
                logger.exception("Failed to refresh open PRs for %s", full_name)
                continue
            for raw in pulls:
                await _upsert_open_pr(session, full_name, raw)
        payload = await build_digest_payload(session)

    slack_sent = False
    client = slack or SlackClient(cfg)
    if client.enabled():
        try:
            slack_sent = await client.post_message(format_digest_text(payload))
        except SlackError:
            logger.exception("Digest Slack post failed")

    digest = DigestRun(
        open_pr_count=payload["open_pr_count"],
        blocker_pr_count=payload["blocker_pr_count"],
        slack_sent=slack_sent,
        payload=payload,
    )
    session.add(digest)
    await session.commit()
    if not slack_sent:
        logger.info("Digest stored without Slack: %s", format_digest_text(payload))
    return digest


async def _upsert_open_pr(session: AsyncSession, full_name: str, raw: dict[str, Any]) -> None:
    repo = (await session.execute(select(Repository).where(Repository.full_name == full_name))).scalar_one_or_none()
    if repo is None:
        repo = Repository(full_name=full_name, enabled=True)
        session.add(repo)
        await session.flush()
    number = int(raw["number"])
    pr = (
        await session.execute(
            select(PullRequest).where(PullRequest.repository_id == repo.id, PullRequest.number == number)
        )
    ).scalar_one_or_none()
    assignee = None
    if raw.get("assignee"):
        assignee = raw["assignee"].get("login")
    if pr is None:
        pr = PullRequest(repository_id=repo.id, number=number)
        session.add(pr)
    pr.html_url = raw.get("html_url") or ""
    pr.title = raw.get("title") or ""
    pr.author = (raw.get("user") or {}).get("login") or ""
    pr.assignee = assignee
    pr.state = "open"
    pr.base_sha = (raw.get("base") or {}).get("sha") or pr.base_sha
    pr.head_sha = (raw.get("head") or {}).get("sha") or pr.head_sha
    pr.head_ref = (raw.get("head") or {}).get("ref") or pr.head_ref
    pr.is_draft = bool(raw.get("draft"))
    await session.flush()
