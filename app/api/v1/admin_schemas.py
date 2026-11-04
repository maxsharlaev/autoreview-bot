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
