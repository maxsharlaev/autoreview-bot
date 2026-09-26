from __future__ import annotations

import uuid
from pathlib import Path

from app.progress import ReviewProgress
from app.security.sandbox import sandbox_env
from app.services.codex_runner import CodexLogFilter, _emit_codex_line, build_codex_command, classify_codex_error
from app.services.constants import CODEX_AUTH, CODEX_QUOTA, CODEX_UNAVAILABLE


def test_codex_command_auto_runs_safe_and_rejects_dangerous() -> None:
    cmd = build_codex_command(
        binary="codex",
        schema_path=Path("/tmp/schema.json"),
        output_path=Path("/tmp/out.json"),
        model="gpt-5.6-sol",
    )
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("--ask-for-approval") + 1] == "never"
    assert cmd.index("--ask-for-approval") < cmd.index("exec")
    assert 'approval_policy="never"' in cmd
    assert 'model_reasoning_effort="medium"' in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "--yolo" not in cmd


def test_sandbox_env_sets_codex_api_key(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "from-process")
    env = sandbox_env("  sk-test-from-settings\n")
    assert env["CODEX_API_KEY"] == "sk-test-from-settings"
    assert env["OPENAI_API_KEY"] == "sk-test-from-settings"
    assert "GITHUB_TOKEN" not in env


def test_classify_codex_quota() -> None:
    assert (
        classify_codex_error("stream disconnected before completion: You have no credits remaining. Add credits")
        == CODEX_QUOTA
    )


def test_classify_codex_auth() -> None:
    assert classify_codex_error("401 Unauthorized: Missing bearer or basic authentication") == CODEX_AUTH


def test_classify_codex_other() -> None:
    assert classify_codex_error("codex CLI is not installed") == CODEX_UNAVAILABLE


def test_emit_codex_line_truncates_json(caplog) -> None:
    caplog.set_level("INFO", logger="app.services.codex_runner")
    _emit_codex_line("out", "{" + "x" * 500)
    assert "json payload" in caplog.text


def test_codex_log_skips_prompt_echo(caplog) -> None:
    caplog.set_level("INFO", logger="app.services.codex_runner")
    filt = CodexLogFilter()
    filt.emit("user")
    filt.emit("You are the Open PR Review agent.")
    filt.emit("<untrusted_pr_title>")
    filt.emit("warning: Codex could not find bubblewrap on PATH. Install bubblewrap")
    filt.emit("exec")
    filt.emit("/bin/bash -lc 'git diff'")
    text = caplog.text
    assert "You are the Open PR Review" not in text
    assert "untrusted_pr_title" not in text
    assert "skipped prompt echo (2 lines)" in text
    assert "bundled bubblewrap" in text
    assert "codex exec /bin/bash" in text
    assert "untrusted_pr_title" not in text


def test_codex_log_skips_exec_diff(caplog) -> None:
    caplog.set_level("INFO", logger="app.services.codex_runner")
    filt = CodexLogFilter()
    filt.emit("exec")
    filt.emit("/bin/bash -lc 'git diff --unified=80'")
    filt.emit("diff --git a/src/pages/Blog/Blog.jsx b/src/pages/Blog/Blog.jsx")
    filt.emit("@@ -3,8 +3,12 @@ import React")
    filt.emit("+  if (!isSupportedBlogLocale(locale)) {")
    filt.emit("exited 0 in 12ms:")
    text = caplog.text
    assert "isSupportedBlogLocale" not in text
    assert "diff --git" not in text
    assert "skipped exec output (3 lines)" in text
    assert "git diff" in text
    assert "exited 0" in text


def test_codex_log_bwrap_userns_once(caplog) -> None:
    caplog.set_level("WARNING", logger="app.services.codex_runner")
    filt = CodexLogFilter()
    msg = (
        "bwrap: No permissions to create a new namespace, likely because the kernel "
        "does not allow non-privileged user namespaces. On e.g. debian this can be "
        "enabled with 'sysctl kernel.unprivileged_userns_clone=1'."
    )
    filt.emit(msg)
    filt.emit(msg)
    filt.emit("exited 1 in 0ms:")
    assert caplog.text.count("cannot create user namespaces") == 1
    assert "exited 1" not in caplog.text


def test_progress_prefix() -> None:
    progress = ReviewProgress(uuid.UUID("7222443c-2058-4bd1-8484-135a55126482"), "org/repo", 12)
    assert progress._prefix() == "[7222443c org/repo#12]"
