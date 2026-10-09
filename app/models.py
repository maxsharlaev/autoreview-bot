from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def extract_comment_author_logins(comment_authors: list[Any] | None) -> list[str]:
    """Extract login strings from comment_authors in either old or new format.

    Tolerant reader: handles both:
    - Old format: ["login1", "login2"]
    - New format: [{"login": "login1", "owner_id": "default", "kind": null}, ...]
    """
    if not comment_authors:
        return []

    logins = []
    for item in comment_authors:
        if isinstance(item, str):
            logins.append(item)
        elif isinstance(item, dict) and "login" in item:
            logins.append(item["login"])
    return logins


LEGACY_OWNER_ID = "default"


def effective_owner_id(stored_owner_id: str | None, legacy_default_owner: str | None) -> str:
    """Owner a stored row belongs to, mapping legacy 'default' rows to their inheritor.

    legacy_default_owner comes from OwnerRegistry.legacy_default_alias(): None keeps
    'default' rows with the literal 'default' owner.
    """
    owner_id = stored_owner_id or LEGACY_OWNER_ID
    if owner_id == LEGACY_OWNER_ID and legacy_default_owner:
        return legacy_default_owner
    return owner_id


def extract_comment_author_logins_for_owner(
    comment_authors: list[Any] | None,
    owner_id: str,
    legacy_default_owner: str | None = None,
) -> list[str]:
    """Extract login strings for a specific owner only.

    Only returns logins where:
    - Old format (plain strings) and new-format entries with owner_id 'default':
      they belong to the legacy 'default' owner, or to legacy_default_owner when
      another owner inherits legacy rows (see OwnerRegistry.legacy_default_alias)
    - New format: owner_id matches the specified owner

    This ensures comment author deduplication is per-owner.
    """
    if not comment_authors:
        return []

    logins = []
    for item in comment_authors:
        if isinstance(item, str):
            if effective_owner_id(LEGACY_OWNER_ID, legacy_default_owner) == owner_id:
                logins.append(item)
        elif isinstance(item, dict) and "login" in item:
            if effective_owner_id(item.get("owner_id"), legacy_default_owner) == owner_id:
                logins.append(item["login"])
    return logins


def build_comment_author_entry(login: str, owner_id: str = "default", kind: str | None = None) -> dict[str, Any]:
    """Build a comment_authors entry in the new format."""
    return {"login": login, "owner_id": owner_id, "kind": kind}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Repository(TimestampMixin, Base):
    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    policy_profile: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    comment_authors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False, index=True)

    pull_requests: Mapped[list[PullRequest]] = relationship(back_populates="repository")


class PullRequest(TimestampMixin, Base):
    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("repository_id", "number", name="uq_pull_requests_repo_number"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    html_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    bot_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bot_title_source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    author: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    assignee: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    head_ref: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_fork: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    repository: Mapped[Repository] = relationship(back_populates="pull_requests")
    review_runs: Mapped[list[ReviewRun]] = relationship(back_populates="pull_request")
    findings: Mapped[list[Finding]] = relationship(back_populates="pull_request")


class ReviewRun(TimestampMixin, Base):
    __tablename__ = "review_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pull_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pull_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger: Mapped[str] = mapped_column(String(32), default="webhook", nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    arq_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    owner_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False, index=True)

    pull_request: Mapped[PullRequest] = relationship(back_populates="review_runs")
    task_snapshot: Mapped[TaskSnapshot | None] = relationship(back_populates="review_run", uselist=False)


class Finding(TimestampMixin, Base):
    __tablename__ = "findings"
    __table_args__ = (UniqueConstraint("pull_request_id", "owner_id", "stable_id", name="uq_findings_pr_owner_stable"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pull_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pull_requests.id", ondelete="CASCADE"), nullable=False
    )
    first_seen_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    last_seen_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False, index=True)
    stable_id: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    path: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    scenario: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence: Mapped[str] = mapped_column(Text, default="", nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    details_visibility: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    current_status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)

    pull_request: Mapped[PullRequest] = relationship(back_populates="findings")
    transitions: Mapped[list[FindingTransition]] = relationship(back_populates="finding")


class FindingTransition(TimestampMixin, Base):
    __tablename__ = "finding_transitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("findings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, default="", nullable=False)

    finding: Mapped[Finding] = relationship(back_populates="transitions")


class TaskSnapshot(TimestampMixin, Base):
    __tablename__ = "task_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_runs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    jira_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    warning: Mapped[str | None] = mapped_column(String(64), nullable=True)
    comment_posted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    transition_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    review_run: Mapped[ReviewRun] = relationship(back_populates="task_snapshot")


class DigestRun(TimestampMixin, Base):
    __tablename__ = "digest_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    open_pr_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocker_pr_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    slack_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
