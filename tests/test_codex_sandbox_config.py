"""AR-003: sandbox mode gate when allow_unsandboxed is false."""

from __future__ import annotations

import pytest
from app.config import CodexYaml
from app.security.sandbox import SAFE_SANDBOX_MODE, validate_review_sandbox
from pydantic import ValidationError


def test_validate_review_sandbox_accepts_read_only() -> None:
    assert validate_review_sandbox("read-only", allow_unsandboxed=False) == SAFE_SANDBOX_MODE
    assert validate_review_sandbox(" read-only ", allow_unsandboxed=False) == SAFE_SANDBOX_MODE


@pytest.mark.parametrize("mode", ["danger-full-access", "workspace-write", "Danger-Full-Access"])
def test_validate_review_sandbox_rejects_unsafe_when_closed(mode: str) -> None:
    with pytest.raises(ValueError, match="allow_unsandboxed"):
        validate_review_sandbox(mode, allow_unsandboxed=False)


@pytest.mark.parametrize("mode", ["danger-full-access", "workspace-write", "read-only"])
def test_validate_review_sandbox_escape_hatch_allows_override(mode: str) -> None:
    assert validate_review_sandbox(mode, allow_unsandboxed=True) == mode


def test_codex_yaml_rejects_danger_full_access_by_default() -> None:
    with pytest.raises(ValidationError, match="allow_unsandboxed"):
        CodexYaml(sandbox="danger-full-access")


def test_codex_yaml_rejects_workspace_write_by_default() -> None:
    with pytest.raises(ValidationError, match="allow_unsandboxed"):
        CodexYaml(sandbox="workspace-write")


def test_codex_yaml_accepts_read_only_by_default() -> None:
    cfg = CodexYaml()
    assert cfg.sandbox == "read-only"
    assert cfg.allow_unsandboxed is False


def test_codex_yaml_escape_hatch_allows_danger_full_access() -> None:
    cfg = CodexYaml(sandbox="danger-full-access", allow_unsandboxed=True)
    assert cfg.sandbox == "danger-full-access"
    assert cfg.allow_unsandboxed is True
