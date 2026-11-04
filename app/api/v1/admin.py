"""Admin API: session auth, users CRUD, read-only runs/queue."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.admin_auth import ADMIN_SESSION_COOKIE, AdminPrincipal, AdminPrincipalDep, require_admin_role
from app.api.deps import SessionDep
from app.api.v1.admin_schemas import (
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
from app.models import AdminUser, PullRequest, Repository, ReviewRun
from app.services.admin_passwords import (
    MIN_ADMIN_PASSWORD_LENGTH,
    aget_root_password_hash,
    ahash_password,
    averify_password,
    password_too_short,
    run_argon2,
)
from app.services.admin_root_crypto import get_root_fingerprint
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

router = APIRouter()

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
    root_login = (settings.admin_root_login or "").strip()
    authenticated_kind: str | None = None
    role = "admin"
    user_id: str | None = None
    session_version: int | None = None
    root_fingerprint: str | None = None
    db_user: AdminUser | None = None

    if root_login and _secure_eq(login_name, root_login):
        expected = settings.admin_root_password or ""
        # Same single argon2 verify as the DB-user path (no plaintext compare timing gap).
        root_hash = await aget_root_password_hash(expected) if expected else _DUMMY_HASH
        ok = await averify_password(root_hash, password)
        if expected and ok:
            authenticated_kind = "root"
            role = "admin"
            user_id = None
            root_fingerprint = await run_argon2(get_root_fingerprint, root_login, expected)
    else:
        db_user = (await session.execute(select(AdminUser).where(AdminUser.login == login_name))).scalar_one_or_none()
        pw_hash = db_user.password_hash if db_user is not None else _DUMMY_HASH
        ok = await averify_password(pw_hash, password)
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
