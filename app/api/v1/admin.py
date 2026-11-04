"""Admin API: session auth, users CRUD, read-only runs/queue."""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.adapters.github import GitHubAppClient, GitHubError
from app.api.admin_auth import ADMIN_SESSION_COOKIE, AdminPrincipal, AdminPrincipalDep, require_admin_role
from app.api.deps import SessionDep
from app.api.v1.admin_schemas import (
    AdminCommentCleanupRequest,
    AdminFindingDetail,
    AdminFindingItem,
    AdminFindingListResponse,
    AdminFindingSuppressRequest,
    AdminFindingUnsuppressRequest,
    AdminLoginRequest,
    AdminMeResponse,
    AdminPasswordResetRequest,
    AdminQueueResponse,
    AdminRunItem,
    AdminRunListResponse,
    AdminUserCreateRequest,
    AdminUserListResponse,
    AdminUserPublic,
    AdminUserUpdateRequest,
)
from app.config import get_settings
from app.metrics import ops_snapshot
from app.models import AdminUser, Finding, PullRequest, Repository, ReviewRun
from app.owners.registry import get_owner_registry
from app.services.admin_passwords import (
    MIN_ADMIN_PASSWORD_LENGTH,
    ahash_password,
    averify_password,
    password_too_short,
)
from app.services.admin_root_crypto import get_root_credentials
from app.services.admin_sessions import (
    check_and_bump_rate_limit,
    create_session,
    destroy_session,
)
from app.services.admin_user_ops import (
    AdminLoginConflictError,
    LastActiveAdminError,
    create_admin_user,
    reset_password_atomic,
    update_user_atomic,
)
from app.services.finding_status import FINDING_STATUS_OPEN, SUPPRESSED_FINDING_STATUSES

router = APIRouter()
logger = logging.getLogger(__name__)

ADMIN_PREFIX = "/api/v1/admin"
_NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def mount_admin_api(app: FastAPI) -> None:
    """Mount the admin router; every response under the prefix (errors included) is no-store."""
    app.include_router(router, prefix=ADMIN_PREFIX, tags=["admin"])

    @app.middleware("http")
    async def _admin_no_store(request: Request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        if request.url.path == ADMIN_PREFIX or request.url.path.startswith(ADMIN_PREFIX + "/"):
            response.headers.update(_NO_STORE_HEADERS)
        return response


_MAX_LIMIT = 100
_DEFAULT_LIMIT = 50
_GENERIC_AUTH_ERROR = "invalid credentials"
_DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_RATE_LIMIT_ERROR = "too many attempts"


def _secure_eq(left: str, right: str) -> bool:
    """Length-safe compare for secrets.compare_digest (raises on length mismatch)."""
    if len(left) != len(right):
        return False
    return secrets.compare_digest(left, right)


def ensure_json_content_type(request: Request) -> None:
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="application/json required")


JsonContentType = Annotated[None, Depends(ensure_json_content_type)]


def _client_ip(request: Request) -> str:
    """Peer address as seen by uvicorn (use --proxy-headers behind a trusted reverse proxy)."""
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=token,
        max_age=settings.admin_session_ttl_seconds,
        httponly=True,
        samesite="strict",
        secure=settings.admin_cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=ADMIN_SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="strict",
        secure=settings.admin_cookie_secure,
    )


def _user_public(user: AdminUser) -> AdminUserPublic:
    return AdminUserPublic(
        id=user.id,
        login=user.login,
        role=user.role,  # type: ignore[arg-type]
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def _to_run_item(run: ReviewRun) -> AdminRunItem:
    pr = run.pull_request
    repo = pr.repository if pr is not None else None
    return AdminRunItem(
        id=run.id,
        owner_id=run.owner_id,
        repository=repo.full_name if repo is not None else "",
        pull_request_number=pr.number if pr is not None else 0,
        head_sha=run.head_sha or "",
        status=run.status,
        trigger=run.trigger,
        created_at=run.created_at,
        updated_at=run.updated_at,
        comment_url=run.comment_url,
        has_sensitive_content=run.has_sensitive_content,
        error_code=run.error_code,
        skip_reason=run.skip_reason,
        comment_cleaned_by=run.comment_cleaned_by,
        comment_cleaned_at=run.comment_cleaned_at,
        comment_cleanup_reason=run.comment_cleanup_reason,
    )


@router.post("/login")
async def login(
    request: Request,
    session: SessionDep,
    _: JsonContentType,
    body: AdminLoginRequest,
) -> Response:
    """Authenticate root (env) or DB user; set HttpOnly session cookie."""
    redis = request.app.state.redis
    settings = get_settings()

    allowed = await check_and_bump_rate_limit(
        redis,
        login=body.login,
        ip=_client_ip(request),
        per_login=settings.admin_login_rate_limit_per_login,
        per_ip=settings.admin_login_rate_limit_per_ip,
        window_seconds=settings.admin_login_rate_window_seconds,
    )
    if not allowed:
        raise HTTPException(status_code=429, detail=_RATE_LIMIT_ERROR)

    login_name = body.login.strip()
    password = body.password
    authenticated_kind: str | None = None
    role = "admin"
    user_id: str | None = None
    session_version: int | None = None
    root_fingerprint: str | None = None
    db_user: AdminUser | None = None

    # Every attempt (root included) does the same work: one DB lookup, then one argon2 verify.
    found = (await session.execute(select(AdminUser).where(AdminUser.login == login_name))).scalar_one_or_none()
    root = get_root_credentials()
    is_root = root is not None and _secure_eq(login_name, root.login)
    if is_root:
        # Root takes precedence over a same-named DB row (startup also rejects that collision).
        pw_hash = root.password_hash
    else:
        db_user = found
        pw_hash = db_user.password_hash if db_user is not None else _DUMMY_HASH
    ok = await averify_password(pw_hash, password)

    if is_root:
        if ok:
            authenticated_kind = "root"
            role = "admin"
            user_id = None
            root_fingerprint = root.fingerprint
    else:
        if db_user is not None and db_user.is_active and ok and db_user.role in ("admin", "viewer"):
            authenticated_kind = "user"
            role = db_user.role
            user_id = str(db_user.id)
            session_version = int(db_user.session_version)

    if authenticated_kind is None:
        raise HTTPException(status_code=401, detail=_GENERIC_AUTH_ERROR)

    if authenticated_kind == "user" and db_user is not None:
        db_user.last_login_at = datetime.now(UTC)
        await session.commit()

    sess = await create_session(
        redis,
        kind=authenticated_kind,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
        login=login_name if authenticated_kind == "root" else (db_user.login if db_user else login_name),
        user_id=user_id,
        ttl_seconds=settings.admin_session_ttl_seconds,
        session_version=session_version,
        root_fingerprint=root_fingerprint,
    )
    response = JSONResponse({"ok": True, "role": role, "kind": authenticated_kind})
    _set_session_cookie(response, sess.token)
    return response


@router.post("/logout")
async def logout(
    request: Request,
    _: JsonContentType,
    principal: AdminPrincipalDep,
) -> Response:
    redis = request.app.state.redis
    await destroy_session(redis, principal.session_token)
    response = JSONResponse({"ok": True})
    _clear_session_cookie(response)
    return response


@router.get("/me", response_model=AdminMeResponse)
async def me(principal: AdminPrincipalDep) -> AdminMeResponse:
    return AdminMeResponse(login=principal.login, role=principal.role, kind=principal.kind)


def _admin_only(principal: AdminPrincipal) -> AdminPrincipal:
    return require_admin_role(principal)


@router.get("/users", response_model=AdminUserListResponse)
async def list_users(
    session: SessionDep,
    principal: AdminPrincipalDep,
) -> AdminUserListResponse:
    _admin_only(principal)
    rows = (await session.execute(select(AdminUser).order_by(AdminUser.created_at.asc()))).scalars().all()
    return AdminUserListResponse(items=[_user_public(u) for u in rows])


@router.post("/users", response_model=AdminUserPublic, status_code=201)
async def create_user(
    request: Request,
    session: SessionDep,
    principal: AdminPrincipalDep,
    _: JsonContentType,
    body: AdminUserCreateRequest,
) -> AdminUserPublic:
    _admin_only(principal)
    settings = get_settings()

    login_name = body.login.strip()
    if not login_name:
        raise HTTPException(status_code=422, detail="login is required")
    root_login = (settings.admin_root_login or "").strip()
    if root_login and _secure_eq(login_name, root_login):
        raise HTTPException(status_code=409, detail="login conflicts with admin root")
    if password_too_short(body.password):
        raise HTTPException(
            status_code=422,
            detail=f"password must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters",
        )
    existing = (await session.execute(select(AdminUser.id).where(AdminUser.login == login_name))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="login already exists")

    user = AdminUser(
        id=uuid.uuid4(),
        login=login_name,
        password_hash=await ahash_password(body.password),
        role=body.role,
        is_active=True,
        session_version=0,
    )
    try:
        await create_admin_user(session, user)
    except AdminLoginConflictError:
        raise HTTPException(status_code=409, detail="login already exists") from None
    return _user_public(user)


@router.patch("/users/{user_id}", response_model=AdminUserPublic)
async def update_user(
    user_id: uuid.UUID,
    session: SessionDep,
    principal: AdminPrincipalDep,
    _: JsonContentType,
    body: AdminUserUpdateRequest,
) -> AdminUserPublic:
    _admin_only(principal)

    user = (await session.execute(select(AdminUser).where(AdminUser.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")

    new_role = None
    new_active = None
    bump = False
    if body.role is not None and body.role != user.role:
        new_role = body.role
        bump = True
    if body.is_active is not None and body.is_active != user.is_active:
        new_active = body.is_active
        if not body.is_active:
            bump = True

    if new_role is None and new_active is None:
        return _user_public(user)

    rootless = not (get_settings().admin_root_login or "").strip()
    try:
        updated = await update_user_atomic(
            session,
            user_id=user_id,
            role=new_role,
            is_active=new_active,
            bump_session=bump,
            protect_last_admin=rootless,
        )
    except LastActiveAdminError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="cannot demote or deactivate the last active admin") from None
    await session.commit()
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")
    return _user_public(updated)


@router.post("/users/{user_id}/reset-password", response_model=AdminUserPublic)
async def reset_password(
    user_id: uuid.UUID,
    session: SessionDep,
    principal: AdminPrincipalDep,
    _: JsonContentType,
    body: AdminPasswordResetRequest,
) -> AdminUserPublic:
    _admin_only(principal)
    if password_too_short(body.password):
        raise HTTPException(
            status_code=422,
            detail=f"password must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters",
        )
    updated = await reset_password_atomic(
        session,
        user_id=user_id,
        password_hash=await ahash_password(body.password),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="user not found")
    await session.commit()
    return _user_public(updated)


@router.get("/runs", response_model=AdminRunListResponse)
async def list_runs(
    session: SessionDep,
    _: AdminPrincipalDep,
    owner_id: Annotated[str | None, Query()] = None,
    repo: Annotated[str | None, Query(description="Repository full_name")] = None,
    status: Annotated[str | None, Query()] = None,
    sensitive: Annotated[bool | None, Query(description="has_sensitive_content")] = None,
    cleaned: Annotated[bool | None, Query(description="comment cleaned by an admin")] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminRunListResponse:
    owner = (owner_id or "").strip() or None
    repo_name = (repo or "").strip() or None
    st = (status or "").strip() or None
    filters: list[Any] = []
    if owner:
        filters.append(ReviewRun.owner_id == owner)
    if st:
        filters.append(ReviewRun.status == st)
    if repo_name:
        filters.append(Repository.full_name == repo_name)
    if sensitive is not None:
        filters.append(
            ReviewRun.has_sensitive_content.is_(True) if sensitive else ReviewRun.has_sensitive_content.isnot(True)
        )
    if cleaned is not None:
        filters.append(ReviewRun.comment_cleaned_at.isnot(None) if cleaned else ReviewRun.comment_cleaned_at.is_(None))

    base = (
        select(ReviewRun)
        .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
        .join(Repository, PullRequest.repository_id == Repository.id)
        .options(selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository))
    )
    if filters:
        base = base.where(*filters)

    count_q = (
        select(func.count())
        .select_from(ReviewRun)
        .join(PullRequest, ReviewRun.pull_request_id == PullRequest.id)
        .join(Repository, PullRequest.repository_id == Repository.id)
    )
    if filters:
        count_q = count_q.where(*filters)
    total = int((await session.execute(count_q)).scalar_one())
    rows = (
        (await session.execute(base.order_by(ReviewRun.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    )
    return AdminRunListResponse(
        items=[_to_run_item(run) for run in rows],
        limit=limit,
        offset=offset,
        count=total,
    )


@router.get("/runs/{review_run_id}", response_model=AdminRunItem)
async def get_run(
    review_run_id: uuid.UUID,
    session: SessionDep,
    _: AdminPrincipalDep,
    owner_id: Annotated[str | None, Query()] = None,
) -> AdminRunItem:
    run = (
        await session.execute(
            select(ReviewRun)
            .options(selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository))
            .where(ReviewRun.id == review_run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="review run not found")
    owner = (owner_id or "").strip()
    if owner and run.owner_id != owner:
        raise HTTPException(status_code=404, detail="review run not found")
    return _to_run_item(run)


_FINDING_NOT_FOUND = "finding not found"


def _finding_fields(finding: Finding) -> dict[str, Any]:
    pr = finding.pull_request
    repo = pr.repository if pr is not None else None
    return {
        "id": finding.id,
        "owner_id": finding.owner_id,
        "repository": repo.full_name if repo is not None else "",
        "pull_request_number": pr.number if pr is not None else 0,
        "stable_id": finding.stable_id,
        "severity": finding.severity,
        "category": finding.category,
        "path": finding.path,
        "line": finding.line,
        "title": finding.title,
        "current_status": finding.current_status,
        "first_seen_run_id": finding.first_seen_run_id,
        "last_seen_run_id": finding.last_seen_run_id,
        "status_changed_by": finding.status_changed_by,
        "status_changed_at": finding.status_changed_at,
        "status_reason": finding.status_reason,
        "created_at": finding.created_at,
        "updated_at": finding.updated_at,
    }


def _finding_detail(finding: Finding) -> AdminFindingDetail:
    return AdminFindingDetail(
        **_finding_fields(finding),
        scenario=finding.scenario,
        evidence=finding.evidence,
        recommendation=finding.recommendation,
    )


async def _load_finding(session: Any, finding_id: uuid.UUID) -> Finding | None:
    return (
        await session.execute(
            select(Finding)
            .options(selectinload(Finding.pull_request).selectinload(PullRequest.repository))
            .where(Finding.id == finding_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


@router.get("/findings", response_model=AdminFindingListResponse)
async def list_findings(
    session: SessionDep,
    _: AdminPrincipalDep,
    owner_id: Annotated[str | None, Query()] = None,
    repo: Annotated[str | None, Query(description="Repository full_name")] = None,
    pr: Annotated[int | None, Query(ge=1, description="Pull request number")] = None,
    status: Annotated[list[str] | None, Query()] = None,
    severity: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminFindingListResponse:
    filters: list[Any] = []
    if (owner_id or "").strip():
        filters.append(Finding.owner_id == owner_id.strip())
    if (repo or "").strip():
        filters.append(Repository.full_name == repo.strip())
    if pr is not None:
        filters.append(PullRequest.number == pr)
    statuses = [s.strip() for s in (status or []) if s.strip()]
    if statuses:
        filters.append(Finding.current_status.in_(statuses))
    if (severity or "").strip():
        filters.append(Finding.severity == severity.strip())

    joined = (
        select(Finding)
        .join(PullRequest, Finding.pull_request_id == PullRequest.id)
        .join(Repository, PullRequest.repository_id == Repository.id)
    )
    count_q = (
        select(func.count())
        .select_from(Finding)
        .join(PullRequest, Finding.pull_request_id == PullRequest.id)
        .join(Repository, PullRequest.repository_id == Repository.id)
    )
    if filters:
        joined = joined.where(*filters)
        count_q = count_q.where(*filters)
    total = int((await session.execute(count_q)).scalar_one())
    rows = (
        (
            await session.execute(
                joined.options(selectinload(Finding.pull_request).selectinload(PullRequest.repository))
                .order_by(Finding.updated_at.desc(), Finding.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return AdminFindingListResponse(
        items=[AdminFindingItem(**_finding_fields(f)) for f in rows], limit=limit, offset=offset, count=total
    )


@router.get("/findings/{finding_id}", response_model=AdminFindingDetail)
async def get_finding(
    finding_id: uuid.UUID,
    session: SessionDep,
    _: AdminPrincipalDep,
    owner_id: Annotated[str | None, Query()] = None,
) -> AdminFindingDetail:
    finding = await _load_finding(session, finding_id)
    owner = (owner_id or "").strip()
    if finding is None or (owner and finding.owner_id != owner):
        raise HTTPException(status_code=404, detail=_FINDING_NOT_FOUND)
    return _finding_detail(finding)


@router.post("/findings/{finding_id}/suppress", response_model=AdminFindingDetail)
async def suppress_finding(
    finding_id: uuid.UUID,
    session: SessionDep,
    _: JsonContentType,
    principal: AdminPrincipalDep,
    body: AdminFindingSuppressRequest,
) -> AdminFindingDetail:
    _admin_only(principal)
    # Single UPDATE; a review run already in flight may still overwrite it (documented residual risk).
    changed = (
        await session.execute(
            update(Finding)
            .where(Finding.id == finding_id)
            .values(
                current_status=body.status,
                status_changed_by=principal.login,
                status_changed_at=func.now(),
                status_reason=(body.reason or "").strip() or None,
            )
            .returning(Finding.id)
        )
    ).scalar_one_or_none()
    if changed is None:
        await session.rollback()
        raise HTTPException(status_code=404, detail=_FINDING_NOT_FOUND)
    await session.commit()
    finding = await _load_finding(session, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=_FINDING_NOT_FOUND)
    return _finding_detail(finding)


@router.post("/findings/{finding_id}/unsuppress", response_model=AdminFindingDetail)
async def unsuppress_finding(
    finding_id: uuid.UUID,
    session: SessionDep,
    _: JsonContentType,
    principal: AdminPrincipalDep,
    body: AdminFindingUnsuppressRequest,
) -> AdminFindingDetail:
    _admin_only(principal)
    changed = (
        await session.execute(
            update(Finding)
            .where(Finding.id == finding_id, Finding.current_status.in_(sorted(SUPPRESSED_FINDING_STATUSES)))
            .values(
                current_status=FINDING_STATUS_OPEN,
                status_changed_by=principal.login,
                status_changed_at=func.now(),
                status_reason=None,
            )
            .returning(Finding.id)
        )
    ).scalar_one_or_none()
    if changed is None:
        await session.rollback()
        exists = await _load_finding(session, finding_id)
        if exists is None:
            raise HTTPException(status_code=404, detail=_FINDING_NOT_FOUND)
        raise HTTPException(status_code=409, detail="finding is not suppressed")
    await session.commit()
    finding = await _load_finding(session, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail=_FINDING_NOT_FOUND)
    return _finding_detail(finding)


_COMMENT_PATH_RE = re.compile(r"/([^/]+)/([^/]+)/(?:pull|issues)/(\d+)")
_COMMENT_FRAGMENT_RE = re.compile(r"issuecomment-(\d+)")


def _comment_id_for_repo(url: str, full_name: str, number: int) -> int | None:
    """Comment id if ``url`` is a github.com comment URL of ``full_name``, else None."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:  # malformed authority, e.g. unmatched IPv6 bracket
        return None
    path = _COMMENT_PATH_RE.fullmatch(parts.path)
    frag = _COMMENT_FRAGMENT_RE.fullmatch(parts.fragment)
    if parts.scheme != "https" or host != "github.com" or path is None or frag is None:
        return None
    if f"{path.group(1)}/{path.group(2)}".lower() != full_name.lower() or int(path.group(3)) != number:
        return None
    return int(frag.group(1))


def _github_for_owner(owner_id: str) -> GitHubAppClient | None:
    ctx = get_owner_registry().get(owner_id)
    if ctx is None:
        return None
    return GitHubAppClient.from_credentials(ctx.github, owner_id=owner_id)


async def _load_run(session: Any, review_run_id: uuid.UUID) -> ReviewRun | None:
    return (
        await session.execute(
            select(ReviewRun)
            .options(selectinload(ReviewRun.pull_request).selectinload(PullRequest.repository))
            .where(ReviewRun.id == review_run_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


@router.post("/runs/{review_run_id}/cleanup-comment", response_model=AdminRunItem)
async def cleanup_run_comment(
    review_run_id: uuid.UUID,
    session: SessionDep,
    _: JsonContentType,
    principal: AdminPrincipalDep,
    body: AdminCommentCleanupRequest,
) -> AdminRunItem:
    """Delete the run's published review comment on GitHub (manual cleanup, no re-publish block)."""
    _admin_only(principal)
    run = await _load_run(session, review_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="review run not found")
    if run.comment_cleaned_at is not None:
        return _to_run_item(run)  # idempotent repeat: no GitHub call
    if not run.comment_url or run.has_sensitive_content is not True:
        raise HTTPException(status_code=409, detail="run has no sensitive comment to clean up")
    repo = run.pull_request.repository if run.pull_request is not None else None
    comment_id = (
        _comment_id_for_repo(run.comment_url, repo.full_name, run.pull_request.number) if repo is not None else None
    )
    if comment_id is None:
        raise HTTPException(status_code=409, detail="comment url not recognized")
    github = _github_for_owner(run.owner_id)
    if github is None:
        raise HTTPException(status_code=409, detail="owner_not_configured")
    owner, name = repo.full_name.split("/", 1)
    try:
        await github.delete_issue_comment(owner, name, comment_id)
    except GitHubError as exc:
        if exc.status_code != 404:  # 404 = already deleted on GitHub: treat as done
            logger.warning("Admin comment cleanup failed for run %s: HTTP %s", run.id, exc.status_code)
            raise HTTPException(status_code=502, detail="github delete failed") from None
    # One comment can be shared by several runs of the PR (sticky): mark them all.
    await session.execute(
        update(ReviewRun)
        .where(
            ReviewRun.pull_request_id == run.pull_request_id,
            ReviewRun.comment_url == run.comment_url,
            ReviewRun.comment_cleaned_at.is_(None),
        )
        .values(
            comment_cleaned_by=principal.login,
            comment_cleaned_at=func.now(),
            comment_cleanup_reason=(body.reason or "").strip() or None,
        )
    )
    await session.commit()
    refreshed = await _load_run(session, review_run_id)
    return _to_run_item(refreshed or run)


@router.get("/queue", response_model=AdminQueueResponse)
async def get_queue(
    request: Request,
    _: AdminPrincipalDep,
    owner_id: Annotated[str | None, Query()] = None,
) -> AdminQueueResponse:
    snap = await ops_snapshot(
        request.app.state.session_factory,
        request.app.state.redis,
        owner_id=owner_id,
    )
    snap["queue_jobs_global"] = snap.pop("queue_jobs")
    return AdminQueueResponse.model_validate(snap)
