"""Validate checkout refs against a configured base directory."""

from __future__ import annotations

import re
from pathlib import Path

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REF_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class CheckoutPathError(ValueError):
    """Raised when a checkout_ref is missing, absolute, or escapes the base dir."""


def validate_job_id(job_id: str) -> str:
    value = (job_id or "").strip()
    if not _JOB_ID_RE.fullmatch(value):
        raise CheckoutPathError("invalid job_id")
    return value


def resolve_checkout(base_dir: Path, checkout_ref: str) -> Path:
    """Return the absolute checkout path under ``base_dir``.

    ``checkout_ref`` must be a relative path with safe path segments (no ``..``,
    no absolute paths, no empty segments). The resolved path must stay inside
    ``base_dir`` (no symlink escape).
    """
    ref = (checkout_ref or "").strip().replace("\\", "/")
    if not ref or ref.startswith("/") or ref.startswith("~"):
        raise CheckoutPathError("checkout_ref must be a relative path under the checkout base")
    parts = [p for p in ref.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise CheckoutPathError("checkout_ref must not contain '..' or be empty")
    for part in parts:
        if not _REF_PART_RE.fullmatch(part):
            raise CheckoutPathError(f"checkout_ref segment not allowed: {part!r}")
    base = base_dir.resolve()
    path = (base.joinpath(*parts)).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise CheckoutPathError("checkout_ref escapes checkout base") from exc
    if not path.is_dir():
        raise CheckoutPathError("checkout path does not exist or is not a directory")
    return path


def job_work_dir(base_dir: Path, job_id: str) -> Path:
    """Per-job writable directory sibling layout: ``{base}/{job_id}/.opr-work``."""
    jid = validate_job_id(job_id)
    base = base_dir.resolve()
    work = (base / jid / ".opr-work").resolve()
    try:
        work.relative_to(base)
    except ValueError as exc:
        raise CheckoutPathError("job work dir escapes checkout base") from exc
    return work
