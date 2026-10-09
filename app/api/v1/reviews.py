from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.adapters.github import GitHubAppClient, GitHubError
from app.api.deps import OwnerSelectorDep, PrincipalDep, SessionDep, get_redis
from app.metrics import record_routing
from app.models import PullRequest, ReviewRun
from app.owners.registry import (
    REJECT_OWNER_REPO_CONFLICT,
    REJECT_REPO_NOT_ALLOWED,
    REJECT_UNKNOWN_OWNER,
    get_owner_registry,
)
from app.services.constants import SKIP_REPO
from app.services.manual_review import ManualReviewRequest, pr_payload_from_request
from app.services.review_enqueue import queue_review

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/reviews", status_code=202)
async def start_review(
    body: ManualReviewRequest,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
    owner_selector: OwnerSelectorDep,
) -> dict:
    full_name = body.repository or ""
    number = body.number or 0

    # Determine effective owner based on principal and selector
    registry = get_owner_registry()
    effective_owner: str | None = None

    if principal.kind == "owner":
        # Owner-scoped key: can only use their own owner
        if owner_selector and owner_selector != principal.owner_id:
            raise HTTPException(status_code=403, detail="owner_forbidden")
        effective_owner = principal.owner_id
    else:
        # Operator key: can specify any owner or use routing
        effective_owner = owner_selector

    # Route to determine owner if not explicitly set
    if effective_owner:
        route = registry.resolve(full_name, explicit_owner=effective_owner)
        if route.rejected():
            record_routing(owner=effective_owner or "", reason=route.reason, rejected=True)
            if route.reason == REJECT_UNKNOWN_OWNER:
                raise HTTPException(status_code=404, detail="unknown_owner")
            if route.reason == REJECT_OWNER_REPO_CONFLICT:
                raise HTTPException(status_code=409, detail="owner_repo_conflict")
            if route.reason == REJECT_REPO_NOT_ALLOWED:
                raise HTTPException(status_code=403, detail=SKIP_REPO)
            raise HTTPException(status_code=403, detail=route.reason)
    else:
        route = registry.resolve(full_name)
        if route.rejected():
            record_routing(owner="", reason=route.reason, rejected=True)
            if route.reason == REJECT_UNKNOWN_OWNER:
                raise HTTPException(status_code=403, detail="unknown_owner")
            if route.reason == REJECT_REPO_NOT_ALLOWED:
                raise HTTPException(status_code=403, detail=SKIP_REPO)
            raise HTTPException(status_code=403, detail=route.reason)

    owner_id = route.owner_id
    record_routing(owner=owner_id, reason=route.reason, rejected=False)

    ctx = registry.get(owner_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="owner_not_configured")

    info = None
    head_sha = body.head_sha or ""
    base_sha = body.base_sha or ""
    is_fork = False
    if not head_sha:
        owner, repo = full_name.split("/", 1)
        # Build GitHub client from owner context
        github = GitHubAppClient.from_credentials(ctx.github, owner_id=owner_id)
        try:
            info = await github.get_pull_request(owner, repo, number)
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=f"github: {exc}") from exc
        full_name = info.full_name
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
        owner_id=owner_id,
    )
    return {
        "status": run.status,
        "review_run_id": str(run.id),
        "head_sha": run.head_sha,
        "trigger": run.trigger,
        "owner": owner_id,
    }


@router.get("/reviews/{review_run_id}")
async def get_review(
    review_run_id: uuid.UUID,
    session: SessionDep,
    principal: PrincipalDep,
) -> dict:
    run = (
        await session.execute(
            select(ReviewRun)
            .options(selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository))
            .where(ReviewRun.id == review_run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="review run not found")

    # Owner-scoped key can only see runs from their owner
    if principal.kind == "owner" and run.owner_id != principal.owner_id:
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
        "owner": run.owner_id,
        "pull_request": {
            "repository": repository,
            "number": pr.number,
            "html_url": pr.html_url,
            "issue_key": pr.issue_key,
        },
    }
