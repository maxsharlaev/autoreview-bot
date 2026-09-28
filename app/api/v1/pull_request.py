from __future__ import annotations

from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Header, HTTPException, Request

from app.api.deps import SessionDep, get_redis
from app.config import get_app_config, get_settings, repo_allowed
from app.security.webhook import authorize_webhook
from app.services.constants import HANDLED_ACTIONS, SKIP_DRAFT, SKIP_FORK, SKIP_REPO
from app.services.review_enqueue import queue_review

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


def classify_pull_request_event(event: str | None, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return a skip response dict, or None if the event should be queued."""
    if event in {None, "ping"}:
        return {"status": "ok", "event": event or "unknown"}
    if event != "pull_request":
        return {"status": "skipped", "reason": "ignored_event"}
    action = payload.get("action")
    if action not in HANDLED_ACTIONS:
        return {"status": "skipped", "reason": "ignored_action"}
    if action == "labeled":
        label = (payload.get("label") or {}).get("name") or ""
        if label.casefold() != get_app_config().size_guard.override_label.casefold():
            return {"status": "skipped", "reason": "ignored_label"}
    full_name = _full_name(payload)
    if not full_name or not repo_allowed(full_name):
        return {"status": "skipped", "reason": SKIP_REPO}
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


@router.post("/pull-request", status_code=202)
async def pull_request_webhook(
    request: Request,
    session: SessionDep,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> dict[str, Any]:
    body = await request.body()
    settings = get_settings()
    if not authorize_webhook(
        api_key=settings.review_api_key,
        webhook_secret=settings.github_webhook_secret,
        authorization=authorization,
        x_api_key=x_api_key,
        body=body,
        signature_header=x_hub_signature_256,
    ):
        raise HTTPException(status_code=401, detail="invalid access key")

    payload = await request.json() if body else {}
    skipped = classify_pull_request_event(x_github_event, payload)
    if skipped is not None:
        return skipped

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
        full_name=_full_name(payload) or "",
        number=number,
        pr_payload=pr_payload,
        head_sha=head_sha,
        base_sha=base_sha,
        is_fork=False,
        trigger="webhook",
        force=payload.get("action") == "labeled",
    )
    return {
        "status": run.status,
        "review_run_id": str(run.id),
    }
