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

    async def fake_run(args, *, cwd, env, token, timeout_seconds=None):  # noqa: ANN001
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


@pytest.mark.asyncio
async def test_run_kills_stalled_git_on_timeout(tmp_path: Path) -> None:
    """ARB-27-013: _run kills the process (group) when timeout_seconds elapses."""
    import sys
    import time

    from app.services.git_clone import GIT_CLONE_FAILED, GitCloneError, _run

    started = time.monotonic()
    with pytest.raises(GitCloneError) as excinfo:
        await _run(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=tmp_path,
            env={},
            token="tok",
            timeout_seconds=0.5,
        )
    elapsed = time.monotonic() - started
    assert excinfo.value.code == GIT_CLONE_FAILED
    assert "timed out" in str(excinfo.value).lower()
    assert elapsed < 15


@pytest.mark.asyncio
async def test_clone_head_enforces_overall_clone_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from app.services.git_clone import GIT_CLONE_FAILED, GitCloneError, clone_head

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_CLONE_TIMEOUT_SECONDS", "1")

    async def slow_run(args, *, cwd, env, token, timeout_seconds=None):  # noqa: ANN001
        # Simulate _run's timeout path after the step budget elapses.
        delay = 0.05 if timeout_seconds is None else min(0.2, float(timeout_seconds))
        await asyncio.sleep(delay)
        raise GitCloneError(
            f"git timed out after {int(timeout_seconds or 1)}s: {' '.join(args[:4])}",
            code=GIT_CLONE_FAILED,
        )

    monkeypatch.setattr("app.services.git_clone._run", slow_run)
    monkeypatch.setattr("app.services.git_clone.neutralize_escaping_symlinks", lambda dest: 0)

    with pytest.raises(GitCloneError) as excinfo:
        await clone_head(owner="o", repo="r", sha="a" * 40, token="tok", pr_number=1)
    assert excinfo.value.code == GIT_CLONE_FAILED
    assert "timed out" in str(excinfo.value).lower()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("ai-review-")]
    assert leftovers == []


@pytest.mark.asyncio
async def test_clone_head_refreshes_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))

    async def fake_run(args, *, cwd, env, token, timeout_seconds=None):  # noqa: ANN001
        return None

    monkeypatch.setattr("app.services.git_clone._run", fake_run)
    monkeypatch.setattr("app.services.git_clone.neutralize_escaping_symlinks", lambda dest: 0)

    import time

    from app.services.git_clone import clone_head

    before = time.time()
    dest = await clone_head(owner="o", repo="r", sha="b" * 40, token="tok", pr_number=1)
    assert dest.stat().st_mtime >= before - 1


@pytest.mark.asyncio
async def test_clone_lease_is_sidecar_outside_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ARB-27-024: lease file is ``<dirname>.lease`` beside the clone, not inside it."""
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))

    async def fake_run(args, *, cwd, env, token, timeout_seconds=None):  # noqa: ANN001
        # Simulate a repo that tracks top-level .lease (would collide with in-tree lease).
        tracked = Path(cwd) / ".lease"
        if not tracked.exists():
            tracked.write_text("from-remote\n", encoding="utf-8")
        return None

    monkeypatch.setattr("app.services.git_clone._run", fake_run)
    monkeypatch.setattr("app.services.git_clone.neutralize_escaping_symlinks", lambda dest: 0)

    from app.launcher.lease import lease_path, stop_directory_lease
    from app.services.git_clone import clone_head

    dest = await clone_head(owner="o", repo="r", sha="a" * 40, token="tok", pr_number=1)
    try:
        sidecar = lease_path(dest)
        assert sidecar.is_file()
        assert sidecar.parent == tmp_path
        assert (dest / ".lease").read_text(encoding="utf-8") == "from-remote\n"
        assert sidecar != dest / ".lease"
    finally:
        await stop_directory_lease(dest)


@pytest.mark.asyncio
async def test_clone_head_cleanup_on_cancel_during_neutralize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ARB-27-025: CancelledError during neutralize removes dir + sidecar lease."""
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))

    async def fake_run(args, *, cwd, env, token, timeout_seconds=None):  # noqa: ANN001
        return None

    import asyncio

    def boom_neutralize(dest: Path) -> int:
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.services.git_clone._run", fake_run)
    monkeypatch.setattr("app.services.git_clone.neutralize_escaping_symlinks", boom_neutralize)

    from app.services.git_clone import clone_head

    with pytest.raises(asyncio.CancelledError):
        await clone_head(owner="o", repo="r", sha="a" * 40, token="tok")
    left_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    left_leases = list(tmp_path.glob("ai-review-*.lease"))
    assert left_dirs == []
    assert left_leases == []
