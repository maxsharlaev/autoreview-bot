from __future__ import annotations

import logging

from app.adapters.github import GitHubAppClient
from app.adapters.jira import JiraClient, JiraError
from app.adapters.slack import SlackClient, SlackError
from app.config import AppConfig, get_app_config
from app.services.comment_render import RenderInput, render_jira_comment, render_slack_review, render_sticky_comment
from app.services.verifier import VerifiedReview

logger = logging.getLogger(__name__)


class Publisher:
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
