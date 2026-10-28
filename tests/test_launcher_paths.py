"""Checkout path validation for the review launcher."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.launcher.paths import CheckoutPathError, resolve_checkout, validate_job_id


def test_validate_job_id_accepts_hex() -> None:
    assert validate_job_id("abc123") == "abc123"


def test_validate_job_id_rejects_slash() -> None:
    with pytest.raises(CheckoutPathError):
        validate_job_id("../x")


def test_resolve_checkout_ok(tmp_path: Path) -> None:
    repo = tmp_path / "job1" / "repo"
    repo.mkdir(parents=True)
    assert resolve_checkout(tmp_path, "job1/repo") == repo.resolve()


def test_resolve_checkout_rejects_traversal(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret"
    outside.mkdir(exist_ok=True)
    with pytest.raises(CheckoutPathError):
        resolve_checkout(tmp_path, "../outside-secret")


def test_resolve_checkout_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(CheckoutPathError):
        resolve_checkout(tmp_path, str(tmp_path / "job1"))


def test_resolve_checkout_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(CheckoutPathError, match="does not exist"):
        resolve_checkout(tmp_path, "missing/repo")
