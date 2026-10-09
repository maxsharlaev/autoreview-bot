from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.adapters.github import GitHubAppClient
from app.adapters.jira import JiraClient, JiraError
from app.adapters.slack import SlackClient, SlackError
from app.config import AppConfig, get_app_config
from app.services.comment_render import RenderInput, render_jira_comment, render_slack_review, render_sticky_comment
from app.services.verifier import VerifiedReview

if TYPE_CHECKING:
    from app.owners.context import OwnerContext

logger = logging.getLogger(__name__)


class Publisher:
    """Publishes review results to GitHub, Jira, and Slack.

    Can be constructed in two ways:
    1. Legacy: Publisher(github, jira, slack, config) - uses provided or default adapters
    2. Owner context: Publisher.from_context(context) - builds adapters from owner bindings

    The from_context method is a stub for M2; in M1 we only ensure the interface is compatible.
    """

    def __init__(
        self,
        github: GitHubAppClient,
        jira: JiraClient | None = None,
        slack: SlackClient | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.github = github
        self.jira = jira or JiraClient()
        self.slack = slack or SlackClient()
        self.config = config or get_app_config()

    @classmethod
    def from_context(
        cls,
        context: OwnerContext,
        previous_comment_authors: tuple[str, ...] = (),
    ) -> Publisher:
        """Build a Publisher from an owner context.

        This is a stub for M2. In M1, we only need backward compatibility.
        M2 will implement building adapters from context.github, context.jira, context.slack.

        IMPORTANT: When the owner has no Jira/Slack binding, we create explicitly disabled
        clients that never read global settings. This prevents cross-owner data leaks.
        """
        github = GitHubAppClient.from_credentials(
            context.github,
            owner_id=context.id,
            previous_comment_authors=previous_comment_authors,
        )
        jira = (
            JiraClient.from_binding(context.jira, context.config)
            if context.jira
            else JiraClient.disabled(context.config)
        )
        slack = (
            SlackClient.from_binding(context.slack, context.config)
            if context.slack
            else SlackClient.disabled(context.config)
        )
        return cls(github=github, jira=jira, slack=slack, config=context.config)

    async def publish(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        render: RenderInput,
        has_blockers: bool,
        issue_key: str | None,
        current_jira_status: str | None,
    ) -> dict[str, bool]:
        jira_comment_enabled = bool(
            issue_key
            and self.jira.project_allowed(issue_key)
            and self.jira.enabled()
            and self.config.features.jira_comment
        )
        slack_enabled = self.slack.enabled()
        render.private_channels_configured = jira_comment_enabled or slack_enabled
        body = render_sticky_comment(render)
        await self.github.upsert_sticky_comment(owner, repo, pr_number, body)

        result = {"github_comment": True, "jira_comment": False, "jira_transition": False, "slack": False}
        if issue_key and self.jira.project_allowed(issue_key) and self.jira.enabled():
            if jira_comment_enabled:
                try:
                    await self.jira.add_comment(issue_key, render_jira_comment(render))
                    result["jira_comment"] = True
                except JiraError:
                    logger.exception("Jira comment failed for %s", issue_key)
            if (
                self.config.features.jira_transition
                and has_blockers
                and current_jira_status
                and current_jira_status.lower() != self.jira.rework_status(issue_key).lower()
            ):
                try:
                    result["jira_transition"] = await self.jira.transition_to(
                        issue_key, self.jira.rework_status(issue_key)
                    )
                except JiraError:
                    logger.exception("Jira transition failed for %s", issue_key)

        if slack_enabled:
            try:
                result["slack"] = await self.slack.post_message(render_slack_review(render))
            except SlackError:
                logger.exception("Slack notification failed")
        return result


def empty_verified(*, files_total: int, files_reviewed: int, skipped: list[str], truncated: bool) -> VerifiedReview:
    return VerifiedReview(
        summary="Review did not produce findings.",
        task_alignment_status="unclear",
        issue_key=None,
        unmet_acceptance_criteria=[],
        findings=[],
        previous_findings=[],
        coverage={
            "files_total": files_total,
            "files_reviewed": files_reviewed,
            "truncated": truncated,
            "skipped_paths": skipped,
        },
    )
