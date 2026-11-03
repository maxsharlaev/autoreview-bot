"""Finding lifecycle statuses and admin suppression helpers (admin MVP PR1)."""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Finding

# Stored on findings.current_status (free-form string column; no DB enum).
FINDING_STATUS_OPEN: Final = "open"
FINDING_STATUS_RESOLVED: Final = "resolved"
FINDING_STATUS_OBSOLETE: Final = "obsolete"
FINDING_STATUS_NEEDS_HUMAN: Final = "needs_human"
FINDING_STATUS_ACCEPTED: Final = "accepted"
FINDING_STATUS_FALSE_POSITIVE: Final = "false_positive"

# Admin suppressions: stay put across runs until cleared; never re-raised as open.
SUPPRESSED_FINDING_STATUSES: Final[frozenset[str]] = frozenset({FINDING_STATUS_ACCEPTED, FINDING_STATUS_FALSE_POSITIVE})

# Treated as still open for carry-forward / model previous context.
STORED_OPEN_FINDING_STATUSES: Final[frozenset[str]] = frozenset(
    {FINDING_STATUS_OPEN, FINDING_STATUS_NEEDS_HUMAN, "still_open"}
)


def is_suppressed_status(status: str | None) -> bool:
    return (status or "") in SUPPRESSED_FINDING_STATUSES


def findings_for_model_previous(findings: list[Finding]) -> list[Finding]:
    """Findings included in Codex previous context (excludes admin suppressions)."""
    return [item for item in findings if not is_suppressed_status(item.current_status)]


def validate_suppression_status(status: str) -> str:
    """Return normalized suppression status or raise ValueError."""
    normalized = (status or "").strip().lower()
    if normalized not in SUPPRESSED_FINDING_STATUSES:
        allowed = ", ".join(sorted(SUPPRESSED_FINDING_STATUSES))
        raise ValueError(f"suppression status must be one of: {allowed}")
    return normalized


async def set_finding_suppression(
    session: AsyncSession,
    *,
    owner_id: str,
    pull_request_id: uuid.UUID,
    stable_id: str,
    status: str,
) -> Finding:
    """Mark a finding suppressed (accepted / false_positive) for later admin API use.

    Does not write finding_transitions (those rows require a review_run_id). Status
    alone drives orchestrator behavior until the next admin or review update.
    """
    target = validate_suppression_status(status)
    oid = (owner_id or "").strip()
    sid = (stable_id or "").strip()
    if not oid:
        raise ValueError("owner_id is required")
    if not sid:
        raise ValueError("stable_id is required")

    finding = await _get_finding(session, owner_id=oid, pull_request_id=pull_request_id, stable_id=sid)
    if finding is None:
        raise LookupError("finding not found")
    finding.current_status = target
    await session.flush()
    return finding


async def clear_finding_suppression(
    session: AsyncSession,
    *,
    owner_id: str,
    pull_request_id: uuid.UUID,
    stable_id: str,
) -> Finding:
    """Clear suppression: set status back to ``open`` so the next run re-evaluates.

    If the finding was not suppressed, still sets ``open`` (idempotent unsuppress).
    """
    oid = (owner_id or "").strip()
    sid = (stable_id or "").strip()
    if not oid:
        raise ValueError("owner_id is required")
    if not sid:
        raise ValueError("stable_id is required")

    finding = await _get_finding(session, owner_id=oid, pull_request_id=pull_request_id, stable_id=sid)
    if finding is None:
        raise LookupError("finding not found")
    finding.current_status = FINDING_STATUS_OPEN
    await session.flush()
    return finding


async def _get_finding(
    session: AsyncSession,
    *,
    owner_id: str,
    pull_request_id: uuid.UUID,
    stable_id: str,
) -> Finding | None:
    result = await session.execute(
        select(Finding).where(
            Finding.pull_request_id == pull_request_id,
            Finding.owner_id == owner_id,
            Finding.stable_id == stable_id,
        )
    )
    return result.scalar_one_or_none()
