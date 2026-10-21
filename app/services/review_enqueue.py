from __future__ import annotations

import logging
import uuid
from typing import Any

from arq.connections import ArqRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.metrics import record_enqueue
from app.models import PullRequest, Repository, ReviewRun
from app.queue import abort_job, enqueue_review, job_is_active
from app.services.constants import ACTIVE_RUN_STATUSES, IN_FLIGHT_STATUSES, INTERNAL_ERROR, QUEUE_UNAVAILABLE
from app.services.issue_key import extract_issue_key
from app.services.pr_description import without_managed_block
from app.services.progress_status import (
    PROGRESS_OPT_OUT,
    SUPERSEDED_OUTCOME,
    ProgressReporter,
    final_outcome,
    locked_state,
)
from app.services.visibility import Visibility

logger = logging.getLogger(__name__)


async def queue_review(
    session: AsyncSession,
    redis: ArqRedis,
    *,
    full_name: str,
    number: int,
    pr_payload: dict[str, Any],
    head_sha: str,
    base_sha: str,
    is_fork: bool,
    trigger: str = "webhook",
    force: bool = False,
    repository_visibility: Visibility | None = None,
    owner_id: str = "default",
    installation_id: int | None = None,
    route_reason: str | None = None,
    progress: ProgressReporter | None = None,
    report_new_run: bool = True,
) -> ReviewRun:
    """Create or reuse a review run and enqueue it.

    The run row is committed before the job is enqueued, so a fast worker always finds it. If
    enqueueing fails, the row is marked failed (QUEUE_UNAVAILABLE) and no external status exists.
    With a progress reporter (webhook runs), the queued status is written after a successful
    enqueue, under a row lock and only while the run is still pending without a status, so it can
    never overwrite a state the worker already reported. In-flight runs it supersedes get their
    status marked cancelled.

    report_new_run=False (a manual request opting out) skips the new run's own status, and the
    worker sees the opt-out in the run summary; superseded runs still get theirs closed.
    """
    repo = (await session.execute(select(Repository).where(Repository.full_name == full_name))).scalar_one_or_none()
    if repo is None:
        repo = Repository(full_name=full_name, enabled=True, owner_id=owner_id)
        session.add(repo)
        await session.flush()
    elif repo.owner_id != owner_id:
        # Repository moved to another owner - update and log
        old_owner = repo.owner_id
        repo.owner_id = owner_id
        logger.info(
            "repository_owner_changed full_name=%s old_owner=%s new_owner=%s",
            full_name,
            old_owner,
            owner_id,
        )

    pr = (
        await session.execute(
            select(PullRequest).where(PullRequest.repository_id == repo.id, PullRequest.number == number)
        )
    ).scalar_one_or_none()
    head = pr_payload.get("head") or {}
    assignee = None
    if pr_payload.get("assignee"):
        assignee = pr_payload["assignee"].get("login")
    if pr is None:
        pr = PullRequest(repository_id=repo.id, number=number)
        session.add(pr)
    pr.html_url = pr_payload.get("html_url") or ""
    pr.title = pr_payload.get("title") or ""
    pr.author = (pr_payload.get("user") or {}).get("login") or ""
    pr.assignee = assignee
    pr.state = pr_payload.get("state") or "open"
    pr.base_sha = base_sha
    pr.head_sha = head_sha
    pr.head_ref = head.get("ref") or ""
    pr.is_draft = bool(pr_payload.get("draft"))
    pr.is_fork = is_fork
    human_title = "" if pr.bot_title and pr.title == pr.bot_title else pr.title
    pr.issue_key = extract_issue_key(human_title, pr.head_ref, without_managed_block(pr_payload.get("body") or ""))
    await session.flush()

    # Only reuse runs from the SAME owner to prevent cross-owner data mixing
    existing = (
        (
            await session.execute(
                select(ReviewRun)
                .where(
                    ReviewRun.pull_request_id == pr.id,
                    ReviewRun.head_sha == head_sha,
                    ReviewRun.owner_id == owner_id,  # Must match owner
                    ReviewRun.status.in_(ACTIVE_RUN_STATUSES),
                )
                .order_by(ReviewRun.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if existing:
        stale = existing.status in IN_FLIGHT_STATUSES and not await job_is_active(redis, existing.arq_job_id)
        if stale:
            existing.status = "failed"
            existing.error_code = INTERNAL_ERROR
            # Its job is gone: close its open status too, so the commit never shows two.
            await _report_cancelled(progress, existing, full_name, number, status="failed", error_code=INTERNAL_ERROR)
            await session.flush()
        elif not force:
            # Check for B->A case: if ownership changed back and another owner has a
            # newer run for this PR, we should NOT reuse our old completed run.
            # This ensures A gets fresh context after B reviewed the same SHA.
            if existing.status == "completed":
                newer_from_other = (
                    await session.execute(
                        select(ReviewRun)
                        .where(
                            ReviewRun.pull_request_id == pr.id,
                            ReviewRun.owner_id != owner_id,
                            ReviewRun.created_at > existing.created_at,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if newer_from_other is not None:
                    logger.info(
                        "skip_reuse_after_owner_change run=%s owner=%s newer_owner=%s %s#%s sha=%s",
                        existing.id,
                        owner_id,
                        newer_from_other.owner_id,
                        full_name,
                        number,
                        head_sha[:12],
                    )
                    # Don't return - fall through to create new run
                else:
                    await session.commit()
                    logger.info(
                        "reuse review_run=%s status=%s %s#%s sha=%s owner=%s",
                        existing.id,
                        existing.status,
                        full_name,
                        number,
                        head_sha[:12],
                        owner_id,
                    )
                    record_enqueue("reused", owner=owner_id)
                    return existing
            else:
                # For non-completed runs (pending, running), still reuse
                await session.commit()
                logger.info(
                    "reuse review_run=%s status=%s %s#%s sha=%s owner=%s",
                    existing.id,
                    existing.status,
                    full_name,
                    number,
                    head_sha[:12],
                    owner_id,
                )
                record_enqueue("reused", owner=owner_id)
                return existing
        elif existing.status in IN_FLIGHT_STATUSES:
            await abort_job(redis, existing.arq_job_id)
            existing.status = "cancelled"
            await _report_cancelled(progress, existing, full_name, number)
            await session.flush()

    # Only cancel in-flight runs from the SAME owner
    in_flight = (
        (
            await session.execute(
                select(ReviewRun).where(
                    ReviewRun.pull_request_id == pr.id,
                    ReviewRun.owner_id == owner_id,  # Must match owner
                    ReviewRun.status.in_(IN_FLIGHT_STATUSES),
                    ReviewRun.head_sha != head_sha,
                )
            )
        )
        .scalars()
        .all()
    )
    for old in in_flight:
        await abort_job(redis, old.arq_job_id)
        old.status = "cancelled"
        await _report_cancelled(progress, old, full_name, number)

    metric_keys = ("commits", "additions", "deletions", "changed_files")
    summary: dict[str, Any] = {}
    if trigger == "webhook" and all(pr_payload.get(key) is not None for key in metric_keys):
        summary["size_metrics"] = {key: int(pr_payload[key]) for key in metric_keys}
    if repository_visibility is not None:
        summary["repository_visibility"] = repository_visibility
    if installation_id is not None:
        summary["installation_id"] = installation_id
    if route_reason is not None:
        summary["route_reason"] = route_reason
    if not report_new_run:
        summary[PROGRESS_OPT_OUT] = True

    run = ReviewRun(
        id=uuid.uuid4(),
        pull_request_id=pr.id,
        trigger=trigger,
        base_sha=base_sha,
        head_sha=head_sha,
        status="pending",
        summary=summary or None,
        owner_id=owner_id,
    )
    session.add(run)
    await session.commit()
    try:
        job_id = await enqueue_review(redis, str(run.id))
    except Exception:
        logger.exception("enqueue failed review_run=%s %s#%s owner=%s", run.id, full_name, number, owner_id)
        await _mark_enqueue_failed(session, run)
        raise
    run.arq_job_id = job_id
    await session.commit()
    if progress is not None and head_sha and report_new_run:
        await _report_queued(session, progress, run, full_name)
    logger.info(
        "queued review_run=%s %s#%s sha=%s trigger=%s owner=%s",
        run.id,
        full_name,
        number,
        head_sha[:12],
        trigger,
        owner_id,
    )
    record_enqueue("queued", owner=owner_id)
    return run


async def _mark_enqueue_failed(session: AsyncSession, run: ReviewRun) -> None:
    run_id = run.id
    try:
        run.status = "failed"
        run.error_code = QUEUE_UNAVAILABLE
        await session.commit()
    except Exception as exc:
        logger.warning("could not mark review_run=%s failed after enqueue error: %s", run_id, exc)


async def _report_queued(session: AsyncSession, progress: ProgressReporter, run: ReviewRun, full_name: str) -> None:
    """Best-effort queued status; skipped when the worker already took the run over."""
    created: dict[str, Any] | None = None
    run_id, sha = run.id, run.head_sha
    try:
        found, status, state = await locked_state(session, run.id)
        if not found or status != "pending" or state is not None:
            await session.commit()  # release the lock; the worker owns the status now
            return
        await progress.queued(run, full_name)
        created = run.progress_status
        await session.commit()
    except Exception as exc:
        logger.warning("progress status queued write failed for run %s (ignored): %s", run_id, exc)
        try:
            await session.rollback()
        except Exception:
            pass
        if created and created.get("check_run_id"):
            # The check run exists but was not recorded, so the worker will create its own. Close
            # this one instead of leaving it queued. (A commit status has no id: the worker's next
            # write on the same context replaces it.)
            await progress.finish(
                full_name=full_name,
                number=None,
                sha=sha,
                run_id=str(run_id),
                state=created,
                outcome=SUPERSEDED_OUTCOME,
            )


async def _report_cancelled(
    progress: ProgressReporter | None,
    run: ReviewRun,
    full_name: str,
    number: int,
    *,
    status: str = "cancelled",
    error_code: str | None = None,
) -> None:
    state = run.progress_status
    if progress is None or not state or state.get("state") == "completed":
        return
    outcome = final_outcome(
        status=status,
        error_code=error_code,
        skip_reason=None,
        summary=None,
        comment_url=None,
        issue_key=None,
        visibility="public",
        config=progress.config,
    )
    run.progress_status = await progress.finish(
        full_name=full_name, number=number, sha=run.head_sha, run_id=str(run.id), state=state, outcome=outcome
    )
