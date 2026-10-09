from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

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

    # Git-specific variables
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = str(askpass)
    env["GIT_PASSWORD"] = token
    env["GCM_INTERACTIVE"] = "never"

    return env


async def _run(args: list[str], *, cwd: Path, env: dict[str, str], token: str) -> None:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        process.kill()
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
    dest = Path(tempfile.mkdtemp(prefix="ai-review-"))
    origin = public_github_url(owner, repo)
    askpass = dest.parent / f"{dest.name}.askpass"
    askpass.write_text(_ASKPASS, encoding="utf-8")
    askpass.chmod(askpass.stat().st_mode | stat.S_IEXEC)
    env = _git_env(token, askpass)
    git = ["git", "-c", "credential.helper="]
    specs = _fetch_specs(sha, pr_number=pr_number, head_ref=head_ref)
    logger.info("git fetch %s/%s specs=%s", owner, repo, specs)
    last_error: GitCloneError | None = None
    try:
        await _run([*git, "init", "-q"], cwd=dest, env=env, token=token)
        await _run([*git, "remote", "add", "origin", origin], cwd=dest, env=env, token=token)
        fetched = False
        for spec in specs:
            try:
                logger.info("git fetch origin %s", spec)
                await _run([*git, "fetch", "--depth", "1", "origin", spec], cwd=dest, env=env, token=token)
                fetched = True
                break
            except GitCloneError as exc:
                last_error = exc
                logger.warning("git fetch origin %s failed: %s", spec, exc)
        if not fetched:
            assert last_error is not None
            raise last_error
        try:
            await _run([*git, "checkout", "-q", sha], cwd=dest, env=env, token=token)
        except GitCloneError:
            await _run([*git, "checkout", "-q", "FETCH_HEAD"], cwd=dest, env=env, token=token)
        await _run([*git, "remote", "set-url", "origin", origin], cwd=dest, env=env, token=token)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        askpass.unlink(missing_ok=True)
        env.pop("GIT_PASSWORD", None)
    logger.info("git checkout ready %s/%s@%s", owner, repo, sha[:12])
    return dest


def cleanup_checkout(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
