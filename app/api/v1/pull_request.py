from __future__ import annotations

import logging
from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.deps import SessionDep, get_redis
from app.config import get_app_config
from app.metrics import record_routing
from app.owners.registry import (
    REJECT_OWNER_REPO_CONFLICT,
    REJECT_REPO_NOT_ALLOWED,
    REJECT_UNKNOWN_OWNER,
    get_owner_registry,
)
from app.security.webhook import verify_github_signature, verify_github_signature_multi
from app.security.webhook_config import (
    get_normalized_secret,
    get_owner_webhook_secrets,
    is_webhook_enabled,
)
from app.services.constants import HANDLED_ACTIONS, SKIP_DRAFT, SKIP_FORK, SKIP_REPO
from app.services.review_enqueue import queue_review
from app.services.visibility import repository_visibility

logger = logging.getLogger(__name__)

router = APIRouter()


def _full_name(payload: dict[str, Any]) -> str | None:
    repo = payload.get("repository") or {}
    name = repo.get("full_name")
    if name:
        return str(name)
    owner = (repo.get("owner") or {}).get("login")
    repo_name = repo.get("name")
    if owner and repo_name:
        return f"{owner}/{repo_name}"
    return None


def classify_pull_request_event(
    event: str | None,
    payload: dict[str, Any],
    *,
    override_label: str | None = None,
) -> dict[str, Any] | None:
    """Return a skip response dict, or None if the event should be queued.

    Args:
        event: GitHub event type (from X-GitHub-Event header)
        payload: Webhook payload
        override_label: Size guard override label (from owner's config)
    """
    if event in {None, "ping"}:
        return {"status": "ok", "event": event or "unknown"}
    if event != "pull_request":
        return {"status": "skipped", "reason": "ignored_event"}
    action = payload.get("action")
    if action not in HANDLED_ACTIONS:
        return {"status": "skipped", "reason": "ignored_action"}
    if action == "labeled":
        label = (payload.get("label") or {}).get("name") or ""
        check_label = override_label or get_app_config().size_guard.override_label
        if label.casefold() != check_label.casefold():
            return {"status": "skipped", "reason": "ignored_label"}
    # Note: repo_allowed check is now done after routing to use owner's allowlist
    pr_payload = payload.get("pull_request") or {}
    if pr_payload.get("draft") and action != "ready_for_review":
        return {"status": "skipped", "reason": SKIP_DRAFT}
    head = pr_payload.get("head") or {}
    base = pr_payload.get("base") or {}
    head_repo = head.get("repo") or {}
    base_repo = base.get("repo") or {}
    is_fork = bool(head_repo.get("fork")) or head_repo.get("full_name") != base_repo.get("full_name")
    if is_fork:
        return {"status": "skipped", "reason": SKIP_FORK}
    return None


def _get_installation_id(payload: dict[str, Any]) -> int | None:
    """Extract installation.id from webhook payload."""
    installation = payload.get("installation") or {}
    inst_id = installation.get("id")
    return int(inst_id) if inst_id else None


@router.post("/pull-request", status_code=202)
async def pull_request_webhook(
    request: Request,
    session: SessionDep,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, Any]:
    """Handle webhook from GitHub (multi-owner: verifies against all valid secrets)."""
    if not is_webhook_enabled():
        raise HTTPException(
            status_code=503,
            detail="webhook endpoint disabled: no valid webhook secrets configured",
        )

    body = await request.body()

    # Multi-owner: verify against all owner secrets (constant-time, no early exit)
    owner_secrets = get_owner_webhook_secrets()
    matched_owners: set[str] = set()

    if owner_secrets:
        matched_owners = verify_github_signature_multi(
            secrets=owner_secrets,
            body=body,
            header=x_hub_signature_256,
        )
    else:
        # Fallback to legacy single-secret mode
        if verify_github_signature(
            secret=get_normalized_secret(),
            body=body,
            header=x_hub_signature_256,
        ):
            matched_owners = {"default"}

    if not matched_owners:
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = await request.json() if body else {}

    # Early return for ping/non-PR events (return 200, not 202)
    if x_github_event in {None, "ping"}:
        return JSONResponse(content={"status": "ok", "event": x_github_event or "unknown"}, status_code=200)

    full_name = _full_name(payload)
    if not full_name:
        return JSONResponse(content={"status": "skipped", "reason": SKIP_REPO}, status_code=200)

    installation_id = _get_installation_id(payload)

    # Route to owner
    registry = get_owner_registry()
    route_result = registry.resolve(full_name, installation_id=installation_id)

    if route_result.rejected():
        logger.info(
            "webhook routing rejected repo=%s reason=%s",
            full_name,
            route_result.reason,
        )
        record_routing(owner="", reason=route_result.reason, rejected=True)
        if route_result.reason == REJECT_UNKNOWN_OWNER:
            return JSONResponse(content={"status": "skipped", "reason": "unknown_owner"}, status_code=200)
        if route_result.reason == REJECT_REPO_NOT_ALLOWED:
            return JSONResponse(content={"status": "skipped", "reason": SKIP_REPO}, status_code=200)
        return JSONResponse(content={"status": "skipped", "reason": route_result.reason}, status_code=200)

    owner_id = route_result.owner_id

    # Check if the resolved owner's secret was among the matched ones
    if owner_id not in matched_owners:
        logger.warning(
            "webhook owner_signature_mismatch repo=%s resolved_owner=%s matched_owners=%s",
            full_name,
            owner_id,
            matched_owners,
        )
        record_routing(owner=owner_id, reason="owner_signature_mismatch", rejected=True)
        raise HTTPException(status_code=403, detail="owner_signature_mismatch")

    record_routing(owner=owner_id, reason=route_result.reason, rejected=False)
    logger.info(
        "webhook routed repo=%s owner=%s reason=%s",
        full_name,
        owner_id,
        route_result.reason,
    )

    # Get owner context for config
    ctx = registry.get(owner_id)
    if ctx is None:
        return JSONResponse(content={"status": "skipped", "reason": "owner_not_configured"}, status_code=200)

    # Classify with owner's config
    skipped = classify_pull_request_event(
        x_github_event,
        payload,
        override_label=ctx.config.size_guard.override_label,
    )
    if skipped is not None:
        return JSONResponse(content=skipped, status_code=200)

    pr_payload = payload.get("pull_request") or {}
    head = pr_payload.get("head") or {}
    base = pr_payload.get("base") or {}
    number = int(pr_payload["number"])
    head_sha = head.get("sha") or ""
    base_sha = base.get("sha") or ""
    redis: ArqRedis = get_redis(request)
    run = await queue_review(
        session,
        redis,
        full_name=full_name,
        number=number,
        pr_payload=pr_payload,
        head_sha=head_sha,
        base_sha=base_sha,
        is_fork=False,
        trigger="webhook",
        force=payload.get("action") == "labeled",
        repository_visibility=repository_visibility(payload.get("repository")),
        owner_id=owner_id,
        installation_id=installation_id,
        route_reason=route_result.reason,
    )
    return {
        "status": run.status,
        "review_run_id": str(run.id),
        "owner": owner_id,
    }


@router.post("/pull-request/{owner_id}", status_code=202)
async def pull_request_webhook_for_owner(
    owner_id: str,
    request: Request,
    session: SessionDep,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, Any]:
    """Handle webhook for a specific owner (verifies against that owner's secret only)."""
    registry = get_owner_registry()
    ctx = registry.get(owner_id)

    if ctx is None:
        raise HTTPException(status_code=404, detail="unknown_owner")

    if not ctx.webhook_enabled():
        raise HTTPException(
            status_code=503,
            detail=f"webhook endpoint disabled: no valid webhook secret for owner '{owner_id}'",
        )

    body = await request.body()

    # Verify against this owner's secret only
    if not verify_github_signature(
        secret=ctx.webhook_secret or "",
        body=body,
        header=x_hub_signature_256,
    ):
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = await request.json() if body else {}

    # Early return for ping/non-PR events (return 200, not 202)
    if x_github_event in {None, "ping"}:
        return JSONResponse(content={"status": "ok", "event": x_github_event or "unknown"}, status_code=200)

    full_name = _full_name(payload)
    if not full_name:
        return JSONResponse(content={"status": "skipped", "reason": SKIP_REPO}, status_code=200)

    installation_id = _get_installation_id(payload)

    # Route with explicit owner
    route_result = registry.resolve(full_name, installation_id=installation_id, explicit_owner=owner_id)

    if route_result.rejected():
        logger.info(
            "webhook routing rejected repo=%s owner=%s reason=%s",
            full_name,
            owner_id,
            route_result.reason,
        )
        record_routing(owner=owner_id, reason=route_result.reason, rejected=True)
        if route_result.reason == REJECT_OWNER_REPO_CONFLICT:
            raise HTTPException(status_code=409, detail="owner_repo_conflict")
        if route_result.reason == REJECT_REPO_NOT_ALLOWED:
            return JSONResponse(content={"status": "skipped", "reason": SKIP_REPO}, status_code=200)
        return JSONResponse(content={"status": "skipped", "reason": route_result.reason}, status_code=200)

    record_routing(owner=owner_id, reason=route_result.reason, rejected=False)
    logger.info(
        "webhook routed repo=%s owner=%s reason=%s",
        full_name,
        owner_id,
        route_result.reason,
    )

    # Classify with owner's config
    skipped = classify_pull_request_event(
        x_github_event,
        payload,
        override_label=ctx.config.size_guard.override_label,
    )
    if skipped is not None:
        return JSONResponse(content=skipped, status_code=200)

    pr_payload = payload.get("pull_request") or {}
    head = pr_payload.get("head") or {}
    base = pr_payload.get("base") or {}
    number = int(pr_payload["number"])
    head_sha = head.get("sha") or ""
    base_sha = base.get("sha") or ""
    redis: ArqRedis = get_redis(request)
    run = await queue_review(
        session,
        redis,
        full_name=full_name,
        number=number,
        pr_payload=pr_payload,
        head_sha=head_sha,
        base_sha=base_sha,
        is_fork=False,
        trigger="webhook",
        force=payload.get("action") == "labeled",
        repository_visibility=repository_visibility(payload.get("repository")),
        owner_id=owner_id,
        installation_id=installation_id,
        route_reason=route_result.reason,
    )
    return {
        "status": run.status,
        "review_run_id": str(run.id),
        "owner": owner_id,
    }
