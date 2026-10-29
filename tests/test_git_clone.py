"""Git clone helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.services.git_clone import (
    GIT_FORBIDDEN,
    GIT_REF_NOT_FOUND,
    _fetch_specs,
    _redact,
    classify_git_error,
    public_github_url,
)


def test_public_github_url() -> None:
    assert public_github_url("example-org", "example-repo") == ("https://github.com/example-org/example-repo.git")


def test_redact_strips_token_and_explains_403() -> None:
    message = _redact("fatal: Write access to repository not granted. 403 token=github_pat_secret", "github_pat_secret")
    assert "github_pat_secret" not in message
    assert "***" in message
    assert "Contents" in message


def test_classify_deleted_branch() -> None:
    assert classify_git_error("warning: Could not find remote branch fix/seo-audit to clone.") == GIT_REF_NOT_FOUND


def test_classify_forbidden() -> None:
    assert classify_git_error("remote: Write access to repository not granted.\nerror: 403") == GIT_FORBIDDEN


def test_fetch_specs_prefer_sha_and_pull_ref() -> None:
    specs = _fetch_specs("abc123", pr_number=110, head_ref="fix/seo-audit")
    assert specs[0] == "abc123"
    assert specs[1] == "pull/110/head"
    assert "fix/seo-audit" in specs
    assert specs.index("abc123") < specs.index("fix/seo-audit")


@pytest.mark.asyncio
async def test_clone_head_uses_strict_ai_review_uuid_dirname(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))

    async def fake_run(args, *, cwd, env, token):  # noqa: ANN001
        return None

    monkeypatch.setattr("app.services.git_clone._run", fake_run)
    monkeypatch.setattr("app.services.git_clone.neutralize_escaping_symlinks", lambda dest: 0)

    from app.services.git_clone import clone_head

    dest = await clone_head(
        owner="o",
        repo="r",
        sha="a" * 40,
        token="tok",
        pr_number=1,
    )
    assert dest.parent == tmp_path
    assert dest.name.startswith("ai-review-")
    suffix = dest.name.removeprefix("ai-review-")
    assert len(suffix) == 32
    assert all(c in "0123456789abcdef" for c in suffix)
    assert dest.is_dir()
