from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import stat
import tempfile
import time
import uuid
from pathlib import Path

from app.launcher.timeouts import clone_budget_seconds
from app.services.checkout_safety import neutralize_escaping_symlinks
from app.services.constants import GIT_FORBIDDEN, GIT_REF_NOT_FOUND
from app.services.constants import GITHUB_UNAVAILABLE as GIT_CLONE_FAILED

logger = logging.getLogger(__name__)

_ASKPASS = """#!/bin/sh
case "$1" in
  *[Uu]sername*) echo "x-access-token" ;;
  *) echo "$GIT_PASSWORD" ;;
esac
"""

_HINT_403 = (
    "GitHub denied git clone (403). REST can work while git still fails. "
    "Fine-grained PAT needs Contents on this repo; GitHub's git protocol often "
    "requires Contents: Read and write even for a read-only fetch. "
    "Pull requests: Write is only for the sticky comment."
)


class GitCloneError(RuntimeError):
    def __init__(self, message: str, code: str = GIT_CLONE_FAILED) -> None:
        super().__init__(message)
        self.code = code


def public_github_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


def classify_git_error(message: str) -> str:
    lower = message.lower()
    if "403" in message or "write access" in lower or "not accessible" in lower:
        return GIT_FORBIDDEN
    if any(
        marker in lower
        for marker in (
            "not found",
            "couldn't find remote ref",
            "could not find remote branch",
            "does not exist",
            "no such remote ref",
        )
    ):
        return GIT_REF_NOT_FOUND
    return GIT_CLONE_FAILED


def _redact(message: str, token: str) -> str:
    text = message
    if token:
        text = text.replace(token, "***")
    if "403" in text or "Write access" in text or "not granted" in text:
        text = f"{text}\n{_HINT_403}"
    return text


def _git_env(token: str, askpass: Path) -> dict[str, str]:
    """Build a minimal environment for git subprocess.

    Only includes essential variables for git to function, reducing exposure
    to potentially dangerous inherited environment variables.
    """
    # Start with a minimal env, not a copy of the full environment
    env: dict[str, str] = {}

    # Essential for locating executables
    if "PATH" in os.environ:
        env["PATH"] = os.environ["PATH"]

    # Essential for home directory (SSH, git config)
    if "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]

    # Locale settings for consistent output
    if "LANG" in os.environ:
        env["LANG"] = os.environ["LANG"]
    if "LC_ALL" in os.environ:
        env["LC_ALL"] = os.environ["LC_ALL"]

    # TMP directory
    for tmp_var in ("TMPDIR", "TMP", "TEMP"):
        if tmp_var in os.environ:
            env[tmp_var] = os.environ[tmp_var]

    # Proxy variables (both cases for compatibility)
    for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        if proxy_var in os.environ:
            env[proxy_var] = os.environ[proxy_var]

    # SSL/CA certificate variables for git
    for ssl_var in ("SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO"):
        if ssl_var in os.environ:
            env[ssl_var] = os.environ[ssl_var]

    # Git-specific variables
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = str(askpass)
    env["GIT_PASSWORD"] = token
    env["GCM_INTERACTIVE"] = "never"

    return env


def _kill_git_process(process: asyncio.subprocess.Process) -> None:
    """Kill the git subprocess and its process group when possible."""
    try:
        if process.returncode is not None:
            return
        if hasattr(os, "killpg") and process.pid:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        process.kill()
    except ProcessLookupError:
        return


async def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    token: str,
    timeout_seconds: float | None = None,
) -> None:
    # start_new_session so we can kill the whole process group on timeout (POSIX).
    kwargs: dict = {
        "cwd": str(cwd),
        "env": env,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    process = await asyncio.create_subprocess_exec(*args, **kwargs)
    try:
        if timeout_seconds is None:
            _stdout, stderr = await process.communicate()
        else:
            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=max(0.1, float(timeout_seconds))
                )
            except TimeoutError as exc:
                _kill_git_process(process)
                with contextlib.suppress(Exception):
                    await process.wait()
                raise GitCloneError(
                    f"git timed out after {int(timeout_seconds)}s: {' '.join(args[:4])}",
                    code=GIT_CLONE_FAILED,
                ) from exc
    except asyncio.CancelledError:
        _kill_git_process(process)
        with contextlib.suppress(Exception):
            await process.wait()
        raise
    if process.returncode != 0:
        raw = stderr.decode("utf-8", errors="replace")[:500]
        message = _redact(raw, token)
        raise GitCloneError(f"git failed ({process.returncode}): {message}", code=classify_git_error(raw))


def _fetch_specs(sha: str, *, pr_number: int | None, head_ref: str | None) -> list[str]:
    """Branch names die after merge/delete; SHA and pull/N/head usually remain."""
    specs = [sha]
    if pr_number:
        specs.append(f"pull/{pr_number}/head")
        specs.append(f"pull/{pr_number}/merge")
    if head_ref and head_ref not in specs:
        specs.append(head_ref)
    return specs


async def clone_head(
    *,
    owner: str,
    repo: str,
    sha: str,
    token: str,
    head_ref: str | None = None,
    pr_number: int | None = None,
) -> Path:
    checkout_root = os.environ.get("OPR_CHECKOUT_BASE", "").strip()
    # Strict name so the launcher reaper can reclaim orphans: ai-review-<uuid4 hex>.
    name = f"ai-review-{uuid.uuid4().hex}"
    if checkout_root:
        root = Path(checkout_root)
        root.mkdir(parents=True, exist_ok=True)
        dest = root / name
        dest.mkdir(parents=False, exist_ok=False)
    else:
        dest = Path(tempfile.gettempdir()) / name
        dest.mkdir(parents=False, exist_ok=False)
    origin = public_github_url(owner, repo)
    askpass = dest.parent / f"{dest.name}.askpass"
    askpass.write_text(_ASKPASS, encoding="utf-8")
    askpass.chmod(askpass.stat().st_mode | stat.S_IEXEC)
    env = _git_env(token, askpass)
    git = ["git", "-c", "credential.helper="]
    specs = _fetch_specs(sha, pr_number=pr_number, head_ref=head_ref)
    logger.info("git fetch %s/%s specs=%s", owner, repo, specs)
    clone_timeout = float(clone_budget_seconds())
    deadline = time.monotonic() + clone_timeout

    async def _run_budgeted(args: list[str]) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitCloneError(
                f"git clone timed out after {int(clone_timeout)}s",
                code=GIT_CLONE_FAILED,
            )
        await _run(args, cwd=dest, env=env, token=token, timeout_seconds=remaining)

    last_error: GitCloneError | None = None
    try:
        await _run_budgeted([*git, "init", "-q"])
        await _run_budgeted([*git, "remote", "add", "origin", origin])
        fetched = False
        for spec in specs:
            try:
                logger.info("git fetch origin %s", spec)
                await _run_budgeted([*git, "fetch", "--depth", "1", "origin", spec])
                fetched = True
                break
            except GitCloneError as exc:
                last_error = exc
                logger.warning("git fetch origin %s failed: %s", spec, exc)
        if not fetched:
            assert last_error is not None
            raise last_error
        try:
            await _run_budgeted([*git, "checkout", "-q", sha])
        except GitCloneError:
            await _run_budgeted([*git, "checkout", "-q", "FETCH_HEAD"])
        await _run_budgeted([*git, "remote", "set-url", "origin", origin])
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        askpass.unlink(missing_ok=True)
        env.pop("GIT_PASSWORD", None)
    removed = neutralize_escaping_symlinks(dest)
    if removed:
        logger.info("neutralized %s escaping symlink(s) in checkout", removed)
    # Refresh mtime so orphan TTL covers the post-clone review window truthfully.
    try:
        os.utime(dest, None)
    except OSError:
        pass
    logger.info("git checkout ready %s/%s@%s", owner, repo, sha[:12])
    return dest


def cleanup_checkout(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
