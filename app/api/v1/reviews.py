from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.adapters.github import GitHubAppClient, GitHubError
from app.api.deps import ApiKeyDep, SessionDep, get_redis
from app.config import repo_allowed
from app.models import PullRequest, ReviewRun
from app.services.constants import SKIP_REPO
from app.services.manual_review import ManualReviewRequest, pr_payload_from_request
from app.services.review_enqueue import queue_review

router = APIRouter()


@router.post("/reviews", status_code=202)
async def start_review(
    body: ManualReviewRequest,
    request: Request,
    session: SessionDep,
    _: ApiKeyDep,
) -> dict:
    full_name = body.repository or ""
    number = body.number or 0
    if not repo_allowed(full_name):
        raise HTTPException(status_code=403, detail=SKIP_REPO)

    info = None
    head_sha = body.head_sha or ""
    base_sha = body.base_sha or ""
    is_fork = False
    if not head_sha:
        owner, repo = full_name.split("/", 1)
        github = GitHubAppClient()
        try:
            info = await github.get_pull_request(owner, repo, number)
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=f"github: {exc}") from exc
        full_name = info.full_name
        if not repo_allowed(full_name):
            raise HTTPException(status_code=403, detail=SKIP_REPO)
        head_sha = info.head_sha
        base_sha = info.base_sha
        is_fork = info.is_fork

    run = await queue_review(
        session,
        get_redis(request),
        full_name=full_name,
        number=info.number if info else number,
        pr_payload=pr_payload_from_request(body, info=info),
        head_sha=head_sha,
        base_sha=base_sha,
        is_fork=is_fork,
        trigger="manual",
        force=body.force,
    )
    return {
        "status": run.status,
        "review_run_id": str(run.id),
        "head_sha": run.head_sha,
        "trigger": run.trigger,
    }


@router.get("/reviews/{review_run_id}")
async def get_review(review_run_id: uuid.UUID, session: SessionDep, _: ApiKeyDep) -> dict:
    run = (
        await session.execute(
            select(ReviewRun)
            .options(selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository))
            .where(ReviewRun.id == review_run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="review run not found")
    pr = run.pull_request
    repository = pr.repository.full_name if pr.repository else None
    return {
        "review_run_id": str(run.id),
        "status": run.status,
        "trigger": run.trigger,
        "head_sha": run.head_sha,
        "base_sha": run.base_sha,
        "error_code": run.error_code,
        "skip_reason": run.skip_reason,
        "duration_ms": run.duration_ms,
        "summary": run.summary,
        "pull_request": {
            "repository": repository,
            "number": pr.number,
            "html_url": pr.html_url,
            "issue_key": pr.issue_key,
        },
    }
