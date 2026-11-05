"""Pydantic models for the admin API (no password hashes / secrets)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.admin_passwords import MAX_ADMIN_LOGIN_LENGTH, MAX_ADMIN_PASSWORD_LENGTH


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=1, max_length=MAX_ADMIN_LOGIN_LENGTH)
    password: str = Field(min_length=1, max_length=MAX_ADMIN_PASSWORD_LENGTH)


class AdminMeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str
    role: Literal["admin", "viewer"]
    kind: Literal["root", "user"]


class AdminUserPublic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    login: str
    role: Literal["admin", "viewer"]
    is_active: bool
    created_at: datetime | None = None
    last_login_at: datetime | None = None


class AdminUserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=1, max_length=MAX_ADMIN_LOGIN_LENGTH)
    password: str = Field(min_length=1, max_length=MAX_ADMIN_PASSWORD_LENGTH)
    role: Literal["admin", "viewer"] = "viewer"


class AdminUserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "viewer"] | None = None
    is_active: bool | None = None


class AdminPasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=MAX_ADMIN_PASSWORD_LENGTH)


class AdminUserListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminUserPublic]


class AdminRunItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    owner_id: str
    repository: str
    pull_request_number: int
    head_sha: str
    status: str
    trigger: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    comment_url: str | None = None
    has_sensitive_content: bool | None = None
    error_code: str | None = None
    skip_reason: str | None = None
    comment_cleaned_by: str | None = None
    comment_cleaned_at: datetime | None = None
    comment_cleanup_reason: str | None = None


class AdminRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminRunItem]
    limit: int
    offset: int
    count: int


class AdminQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_flight: dict[str, int]
    queue_jobs_global: int
    stale_minutes: int
    stale_runs: list[dict[str, Any]] = Field(default_factory=list)
    last_completed: dict[str, Any] | None = None
    last_failures: list[dict[str, Any]] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)


MAX_FINDING_REASON_LENGTH = 1000


class AdminFindingItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    owner_id: str
    repository: str
    pull_request_number: int
    stable_id: str
    severity: str
    category: str
    path: str
    line: int | None = None
    title: str
    current_status: str
    first_seen_run_id: uuid.UUID
    last_seen_run_id: uuid.UUID
    status_changed_by: str | None = None
    status_changed_at: datetime | None = None
    status_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AdminFindingDetail(AdminFindingItem):
    scenario: str
    evidence: str
    recommendation: str


class AdminFindingListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminFindingItem]
    limit: int
    offset: int
    count: int


class AdminFindingSuppressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted", "false_positive"]
    reason: str | None = Field(default=None, max_length=MAX_FINDING_REASON_LENGTH)


class AdminFindingUnsuppressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminCommentCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=MAX_FINDING_REASON_LENGTH)


class AdminSpaceItem(BaseModel):
    """A GitHub installation (space) accessible to the bot."""

    model_config = ConfigDict(extra="forbid")

    id: int
    login: str
    type: Literal["User", "Organization"]


class AdminSpaceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminSpaceItem]


class AdminOpenFindings(BaseModel):
    """Open findings aggregated by severity."""

    model_config = ConfigDict(extra="forbid")

    P0: int = 0
    P1: int = 0
    P2: int = 0
    P3: int = 0


class AdminRepoItem(BaseModel):
    """A repository within a space with aggregated counts."""

    model_config = ConfigDict(extra="forbid")

    full_name: str
    open_pr_count: int
    open_findings: AdminOpenFindings
    last_run_at: datetime | None = None


class AdminRepoListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminRepoItem]


class AdminRepoPRItem(BaseModel):
    """An open pull request in a repository."""

    model_config = ConfigDict(extra="forbid")

    number: int
    title: str
    head_sha_short: str
    last_run_status: str | None = None


class AdminRepoFindingItem(BaseModel):
    """An open finding in a repository."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    stable_id: str
    severity: str
    title: str
    path: str
    line: int | None = None
    pull_request_number: int


class AdminRepoDetailResponse(BaseModel):
    """Detailed repository info with open PRs and findings."""

    model_config = ConfigDict(extra="forbid")

    full_name: str
    open_prs: list[AdminRepoPRItem]
    open_findings: list[AdminRepoFindingItem]
    open_findings_count: int
    limit: int
    offset: int
