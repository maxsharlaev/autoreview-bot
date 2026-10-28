"""Review progress on the PR head commit: a check run (GitHub App) or a commit status (PAT).

Everything here is best effort. Each GitHub call is bounded by progress_status.timeout_seconds
and every error is logged and swallowed, so status reporting can never fail or block a review.

Summaries and descriptions are built only from machine fields (state, skip reason, error code,
finding counts, comment link). Model text, finding details and Jira text never reach them; the
issue key and task alignment are added only for private repositories or when the public
repository policy allows the key.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from app.config import AppConfig
from app.services.constants import (
    GIT_FORBIDDEN,
    GIT_REF_NOT_FOUND,
    GITHUB_UNAVAILABLE,
    HEAD_CHANGED,
    REVIEW_TIMEOUT,
    SKIP_DRAFT,
    SKIP_FORK,
    SKIP_NO_WRITE,
    SKIP_OWNER_CHANGED,
    SKIP_OWNER_DISABLED,
    SKIP_OWNER_NOT_CONFIGURED,
    SKIP_REPO,
    SKIP_TOO_LARGE,
    CODEX_SANDBOX,
)
from app.services.review_time import format_utc, status_line

if TYPE_CHECKING:
    from app.adapters.github import GitHubAppClient

logger = logging.getLogger(__name__)

CHECK_NAME = "autoreview"
STATUS_CONTEXT = "autoreview"
OPEN_STATES = frozenset({"queued", "in_progress"})
# Runs of a concrete PR head that report progress. Scheduled digests review no PR head.
PROGRESS_TRIGGERS = frozenset({"webhook", "manual"})
# review_runs.summary key: a manual request asked for no status on this run.
PROGRESS_OPT_OUT = "progress_status_opt_out"
STALE_MARGIN_SECONDS = 300
_DESCRIPTION_LIMIT = 140

# Review phases shown while a run is open, in pipeline order. The status is written once per
# phase change (never per tick), so a phase timestamp that stops advancing points to a hung run.
PHASE_QUEUED = "queued"
PHASE_FETCHING = "fetching"
PHASE_CONTEXT = "context"
PHASE_ANALYZING = "analyzing"
PHASE_PUBLISHING = "publishing"
PHASE_TEXT = {
    PHASE_QUEUED: "Queued, waiting for a worker…",
    PHASE_FETCHING: "Fetching pull request and diff…",
    PHASE_CONTEXT: "Reading repository context…",
    PHASE_ANALYZING: "Running model analysis…",
    PHASE_PUBLISHING: "Publishing results…",
}
# Fixed step number per phase, shown as "(4/5)". A path that skips a phase keeps the numbers
# (a too-large PR goes from 3/5 to 5/5).
PHASES = tuple(PHASE_TEXT)
PHASE_STEP = {phase: index for index, phase in enumerate(PHASES, start=1)}

Mode = Literal["check_run", "commit_status"]
Conclusion = Literal["success", "neutral", "skipped", "cancelled", "failure"]

SKIP_REASON_TEXT = {
    SKIP_DRAFT: "pull request is a draft",
    SKIP_FORK: "pull requests from forks are not reviewed",
    SKIP_REPO: "repository is not allowed",
    SKIP_NO_WRITE: "author has no write access",
    SKIP_TOO_LARGE: "pull request is too large for AI review (size guard)",
    SKIP_OWNER_NOT_CONFIGURED: "owner is not configured",
    SKIP_OWNER_DISABLED: "owner is disabled",
    SKIP_OWNER_CHANGED: "repository owner changed",
}
_FAILURE_TEXT = {
    REVIEW_TIMEOUT: "the review timed out",
    GITHUB_UNAVAILABLE: "GitHub was unavailable",
    GIT_FORBIDDEN: "the repository could not be cloned",
    GIT_REF_NOT_FOUND: "the head commit could not be fetched",
    CODEX_SANDBOX: "the review sandbox is unavailable",
}


@dataclass
class Outcome:
    state: Literal["queued", "in_progress", "completed"]
    conclusion: Conclusion | None
    title: str
    summary: str
    details_url: str | None = None
    # One-line commit status description; None derives it from the summary.
    description: str | None = None


def phase_line(phase: str, started: datetime) -> str:
    """`[2026-10-24 21:20:05 UTC] (4/5) Running model analysis…`, always within the commit status limit.

    Truncation shortens only the phase text; the timestamp and the step are always kept.
    """
    prefix = f"[{format_utc(started)}] "
    if phase in PHASE_STEP:
        prefix += f"({PHASE_STEP[phase]}/{len(PHASES)}) "
    text = PHASE_TEXT.get(phase, phase)
    room = _DESCRIPTION_LIMIT - len(prefix)
    if len(text) > room:
        text = text[: room - 1] + "…"
    return prefix + text


def phase_outcome(phase: str, started: datetime) -> Outcome:
    line = phase_line(phase, started)
    if phase == PHASE_QUEUED:
        return Outcome("queued", None, "Review queued", line)
    return Outcome("in_progress", None, "Review in progress", line)


def resolve_mode(config: AppConfig, credentials_kind: str | None) -> Mode | None:
    """auto = check run for a GitHub App, commit status for a PAT; None = reporting is off."""
    settings = config.progress_status
    if not settings.enabled or settings.mode == "off":
        return None
    if settings.mode in ("check_run", "commit_status"):
        return settings.mode
    if credentials_kind == "app":
        return "check_run"
    if credentials_kind == "pat":
        return "commit_status"
    return None


def skip_text(reason: str) -> str:
    return SKIP_REASON_TEXT.get(reason, reason)


def _counts_line(counts: dict[str, Any]) -> str:
    return " ".join(f"{level}={int(counts.get(level) or 0)}" for level in ("P0", "P1", "P2", "P3"))


def _needs_attention(counts: dict[str, Any]) -> bool:
    return any(int(counts.get(level) or 0) for level in ("P0", "P1", "P2"))


def _task_line(*, issue_key: str | None, alignment: str | None, visibility: str, config: AppConfig) -> str | None:
    if visibility == "private":
        if not issue_key:
            return None
        return f"Task: {issue_key}" + (f" (alignment: {alignment})" if alignment else "")
    if issue_key and config.public_repos.jira_disclosure in ("key_only", "full"):
        return f"Task: {issue_key}"
    return None


def final_outcome(
    *,
    status: str,
    error_code: str | None,
    skip_reason: str | None,
    summary: dict[str, Any] | None,
    comment_url: str | None,
    issue_key: str | None,
    visibility: str,
    config: AppConfig,
    crashed: BaseException | None = None,
) -> Outcome:
    """Map a review run's final state to a check run conclusion; findings never map to failure."""
    link = f"\n\n[Review comment]({comment_url})" if comment_url else ""
    if crashed is not None and status in ("pending", "running"):
        timed_out = isinstance(crashed, (asyncio.CancelledError, TimeoutError))
        reason = _FAILURE_TEXT[REVIEW_TIMEOUT] if timed_out else "an internal error occurred"
        return Outcome("completed", "failure", "Review failed", f"The review did not finish: {reason}.")
    if status == "completed":
        counts = (summary or {}).get("finding_counts") or {}
        if error_code == SKIP_TOO_LARGE:
            return Outcome(
                "completed", "skipped", "Review skipped", f"Skipped: {skip_text(SKIP_TOO_LARGE)}.{link}", comment_url
            )
        lines = [f"Review published. Findings: {_counts_line(counts)}."]
        task = _task_line(
            issue_key=issue_key,
            alignment=(summary or {}).get("task_alignment"),
            visibility=visibility,
            config=config,
        )
        if task:
            lines.append(task)
        # The commit status description (140 chars) leaves the review time out; the check run
        # summary and the review comment carry it in full.
        description = " ".join(lines)
        reviewed = status_line(summary)
        if reviewed:
            lines.append(reviewed)
        conclusion: Conclusion = "neutral" if _needs_attention(counts) else "success"
        title = "Review published with findings" if conclusion == "neutral" else "Review published"
        return Outcome("completed", conclusion, title, "\n".join(lines) + link, comment_url, description)
    if status == "skipped":
        reason = skip_reason or "unknown"
        return Outcome("completed", "skipped", "Review skipped", f"Skipped: {skip_text(reason)}.{link}", comment_url)
    if status == "cancelled":
        return Outcome("completed", "cancelled", "Review cancelled", "Superseded by a newer push.")
    if status == "failed":
        if error_code == HEAD_CHANGED:
            return Outcome("completed", "cancelled", "Review cancelled", "The head commit changed during the review.")
        reason = _FAILURE_TEXT.get(error_code or "", f"error {error_code or 'unknown'}")
        text = f"The review did not finish: {reason}.{link}"
        return Outcome("completed", "failure", "Review failed", text, comment_url)
    return Outcome("completed", "failure", "Review failed", "The review did not finish: an internal error occurred.")


def _one_line(outcome: Outcome) -> str:
    source = outcome.description if outcome.description is not None else outcome.summary.split("\n\n[")[0]
    text = source.replace("\n", " ").strip()
    return text if len(text) <= _DESCRIPTION_LIMIT else text[: _DESCRIPTION_LIMIT - 1] + "…"


def _commit_state(outcome: Outcome) -> str:
    if outcome.state != "completed":
        return "pending"
    return {"failure": "failure", "cancelled": "error"}.get(outcome.conclusion or "", "success")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ProgressReporter:
    """Writes one owner's progress status with that owner's own GitHub client."""

    def __init__(self, github: GitHubAppClient, mode: Mode, config: AppConfig) -> None:
        self.github = github
        self.mode = mode
        self.config = config
        self.timeout = config.progress_status.timeout_seconds

    @classmethod
    def build(cls, github: Any, config: AppConfig, credentials_kind: str | None) -> ProgressReporter | None:
        mode = resolve_mode(config, credentials_kind)
        if mode is None or github is None:
            return None
        return cls(github, mode, config)

    async def _call(self, what: str, awaitable: Awaitable[Any]) -> Any:
        try:
            return await asyncio.wait_for(awaitable, timeout=self.timeout)
        except Exception as exc:
            logger.warning("progress status %s failed (ignored): %s", what, exc)
            return None

    async def _write(
        self, full_name: str, sha: str, outcome: Outcome, state: dict[str, Any] | None, external_id: str | None
    ) -> dict[str, Any]:
        """Push one state to GitHub and return the updated bookkeeping dict."""
        owner, repo = full_name.split("/", 1)
        state = dict(state or {})
        state.update({"mode": self.mode, "sha": sha, "state": outcome.state, "updated_at": _now()})
        state["conclusion"] = outcome.conclusion
        if self.mode == "commit_status":
            payload: dict[str, Any] = {
                "state": _commit_state(outcome),
                "context": STATUS_CONTEXT,
                "description": _one_line(outcome),
            }
            if outcome.details_url:
                payload["target_url"] = outcome.details_url
            await self._call("commit status", self.github.create_commit_status(owner, repo, sha, payload))
            return state
        payload = {"status": outcome.state, "output": {"title": outcome.title, "summary": outcome.summary}}
        if outcome.state == "completed":
            payload["conclusion"] = outcome.conclusion
            payload["completed_at"] = _now()
        if outcome.details_url:
            payload["details_url"] = outcome.details_url
        check_run_id = state.get("check_run_id")
        if check_run_id:
            await self._call("check run update", self.github.update_check_run(owner, repo, int(check_run_id), payload))
            return state
        payload.update({"name": CHECK_NAME, "head_sha": sha})
        if external_id:
            payload["external_id"] = external_id
        created = await self._call("check run create", self.github.create_check_run(owner, repo, payload))
        if created:
            state["check_run_id"] = int(created)
        return state

    async def _write_phase(
        self, full_name: str, sha: str, phase: str, state: dict[str, Any] | None, external_id: str | None
    ) -> dict[str, Any]:
        started = datetime.now(UTC)
        state = await self._write(full_name, sha, phase_outcome(phase, started), state, external_id)
        state["phase"] = phase
        state["phase_started_at"] = started.isoformat()
        return state

    async def queued(self, run: Any, full_name: str) -> None:
        run.progress_status = await self._write_phase(full_name, run.head_sha, PHASE_QUEUED, None, str(run.id))

    async def phase_state(
        self, full_name: str, sha: str, run_id: str, state: dict[str, Any] | None, phase: str
    ) -> dict[str, Any] | None:
        """Report a new phase of a running review and return the new state; None for a repeated phase."""
        if state and state.get("phase") == phase:
            return None
        return await self._write_phase(full_name, sha, phase, state, run_id)

    async def phase(self, run: Any, full_name: str, phase: str) -> None:
        """Report a new phase on `run.progress_status`; a repeated phase writes nothing."""
        state = await self.phase_state(
            full_name, run.head_sha, str(run.id), getattr(run, "progress_status", None), phase
        )
        if state is not None:
            run.progress_status = state

    async def in_progress(self, run: Any, full_name: str, number: int) -> None:
        state = getattr(run, "progress_status", None)
        state = await self._write_phase(full_name, run.head_sha, PHASE_FETCHING, state, str(run.id))
        if self.config.progress_status.reaction and not state.get("reaction_id"):
            owner, repo = full_name.split("/", 1)
            reaction_id = await self._call("reaction", self.github.add_issue_reaction(owner, repo, number, "eyes"))
            if reaction_id:
                state["reaction_id"] = int(reaction_id)
        run.progress_status = state

    async def finish(
        self, *, full_name: str, number: int | None, sha: str, run_id: str, state: dict | None, outcome: Outcome
    ) -> dict[str, Any]:
        state = await self._write(full_name, sha, outcome, state, run_id)
        reaction_id = state.pop("reaction_id", None)
        if reaction_id and number is not None:
            owner, repo = full_name.split("/", 1)
            await self._call("reaction removal", self.github.delete_issue_reaction(owner, repo, number, reaction_id))
        return state

    async def skipped(self, full_name: str, sha: str, reason: str) -> None:
        """Webhook-level skip (draft, fork, repository not allowed): no review run exists."""
        if not sha:
            return
        await self._write(full_name, sha, skipped_outcome(reason), None, None)


def skipped_outcome(reason: str) -> Outcome:
    return Outcome("completed", "skipped", "Review skipped", f"Skipped: {skip_text(reason)}.")


SUPERSEDED_OUTCOME = Outcome(
    "completed", "cancelled", "Review status replaced", "This status was not recorded; another one tracks the review."
)


async def locked_state(session: Any, run_id: Any) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Lock the run row and read its status and progress state from the database.

    The lock serializes the API's queued write with the worker's writes, so neither side
    creates a second check run or overwrites a newer state. Returns (found, status, state).

    FOR NO KEY UPDATE, not FOR UPDATE: status writers still exclude each other, but the lock
    does not conflict with the KEY SHARE locks that rows referencing the run (task snapshots,
    finding transitions) hold until the review transaction commits. Writers never change the
    run's key.
    """
    from sqlalchemy import select

    from app.models import ReviewRun

    row = (
        await session.execute(
            select(ReviewRun.status, ReviewRun.progress_status)
            .where(ReviewRun.id == run_id)
            .with_for_update(key_share=True)
        )
    ).first()
    if row is None:
        return False, None, None
    return True, row[0], row[1]


def reporter_for_owner(ctx: Any, github: Any = None) -> ProgressReporter | None:
    """Reporter for an owner context; builds the owner's own GitHub client unless one is given."""
    if ctx is None:
        return None
    credentials = getattr(ctx, "github", None)
    kind = getattr(credentials, "kind", None)
    config = getattr(ctx, "config", None)
    if config is None or kind is None:
        return None
    if github is None:
        from app.adapters.github import GitHubAppClient

        github = GitHubAppClient.from_credentials(credentials, owner_id=ctx.id)
    return ProgressReporter.build(github, config, kind)


async def report_webhook_skip(ctx: Any, full_name: str, pr_payload: dict[str, Any], reason: str) -> None:
    """Best-effort skipped status for a webhook the owner signed but that will not be reviewed."""
    try:
        reporter = reporter_for_owner(ctx)
        if reporter is None:
            return
        sha = ((pr_payload.get("head") or {}).get("sha")) or ""
        await reporter.skipped(full_name, sha, reason)
    except Exception as exc:
        logger.warning("progress status skip report failed (ignored): %s", exc)


def stale_cutoff(job_timeout_seconds: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(seconds=job_timeout_seconds + STALE_MARGIN_SECONDS)


def is_stale(state: dict[str, Any] | None, cutoff: datetime) -> bool:
    if not state or state.get("state") not in OPEN_STATES:
        return False
    try:
        updated = datetime.fromisoformat(str(state.get("updated_at")))
    except ValueError:
        return True
    return updated < cutoff


STALE_OUTCOME = Outcome(
    "completed", "failure", "Review timed out", "The review did not report back in time and was marked as timed out."
)


async def cleanup_stale(session: Any, registry: Any, job_timeout_seconds: int, now: datetime | None = None) -> int:
    """Mark statuses left queued/in progress past the job timeout plus a margin as timed out."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models import PullRequest, ReviewRun

    cutoff = stale_cutoff(job_timeout_seconds, now)
    rows = (
        (
            await session.execute(
                select(ReviewRun)
                .options(selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository))
                .where(
                    ReviewRun.progress_status.is_not(None),
                    ReviewRun.progress_status["state"].astext.in_(sorted(OPEN_STATES)),
                    ReviewRun.created_at < cutoff,
                )
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    cleaned = 0
    for run in rows:
        if not is_stale(run.progress_status, cutoff):
            continue
        ctx = registry.get(run.owner_id)
        outcome = STALE_OUTCOME
        if ctx is None and registry.is_disabled(run.owner_id):
            # A disabled owner's status is closed as skipped with that owner's own credentials.
            ctx = registry.disabled_status_context(run.owner_id)
            outcome = skipped_outcome(SKIP_OWNER_DISABLED)
        reporter = reporter_for_owner(ctx)
        state = dict(run.progress_status)
        if reporter is not None:
            state = await reporter.finish(
                full_name=run.pull_request.repository.full_name,
                number=run.pull_request.number,
                sha=run.head_sha,
                run_id=str(run.id),
                state=state,
                outcome=outcome,
            )
        else:
            # No credentials (owner removed from config, or a disabled owner without usable
            # credentials) or reporting turned off: the GitHub status cannot be closed, and other
            # owners' credentials are never used for it. Stop tracking it locally.
            if ctx is None:
                logger.warning(
                    "progress status of run %s (owner %s) cannot be closed on GitHub: no credentials for the owner",
                    run.id,
                    run.owner_id,
                )
            state.update({"state": "completed", "conclusion": outcome.conclusion, "updated_at": _now()})
        run.progress_status = state
        cleaned += 1
    await session.commit()
    return cleaned
