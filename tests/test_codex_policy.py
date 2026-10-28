"""Execution policy: model-key isolation for agent children + execpolicy allowlist."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from app.security.codex_policy import (
    RULES_FILENAME,
    prepare_codex_home,
    rules_path,
    spawned_command_env,
)
from app.security.sandbox import sandbox_env

requires_codex = pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")


def test_prepare_codex_home_writes_shell_environment_policy(tmp_path: Path) -> None:
    parent = sandbox_env("sk-test-model-key-value")
    home = prepare_codex_home(tmp_path / "codex-home", parent)
    config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    policy = config["shell_environment_policy"]
    assert policy["inherit"] == "none"
    assert policy["ignore_default_excludes"] is False
    assert "PATH" in policy["set"]
    assert "HOME" in policy["set"]
    assert "OPENAI_API_KEY" not in policy["set"]
    assert "CODEX_API_KEY" not in policy["set"]
    assert (home / "rules" / RULES_FILENAME).is_file()
    rules = (home / "rules" / RULES_FILENAME).read_text(encoding="utf-8")
    assert 'pattern = ["rg"]' in rules
    assert 'decision = "allow"' in rules
    assert 'pattern = ["python3"]' in rules
    assert 'decision = "forbidden"' in rules


def test_sandbox_env_sets_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    home = tmp_path / "h"
    home.mkdir()
    env = sandbox_env("sk-from-settings", codex_home=home)
    assert env["CODEX_HOME"] == str(home)
    assert env["CODEX_API_KEY"] == "sk-from-settings"
    assert "GITHUB_TOKEN" not in env


def test_planted_program_run_under_policy_cannot_see_model_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A program the agent would spawn cannot read the model key.

    Codex applies ``shell_environment_policy`` from CODEX_HOME to agent-spawned
    commands. We install that policy, apply the same documented algorithm to the
    worker's sandbox env (which still holds the key for Codex itself), run a
    planted probe as that child would, and assert the key is absent.
    """
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    secret = "sk-planted-probe-must-not-see-this-key"
    parent = sandbox_env(secret)
    assert parent["CODEX_API_KEY"] == secret
    assert parent["OPENAI_API_KEY"] == secret

    home = prepare_codex_home(tmp_path / "codex-home", parent)
    child_env = spawned_command_env(parent, home)

    assert "CODEX_API_KEY" not in child_env
    assert "OPENAI_API_KEY" not in child_env
    assert secret not in child_env.values()
    assert "PATH" in child_env
    assert "HOME" in child_env

    probe = tmp_path / "planted_probe"
    probe.write_text(
        "#!/bin/sh\n"
        "echo \"OPENAI_API_KEY=${OPENAI_API_KEY-<unset>}\"\n"
        "echo \"CODEX_API_KEY=${CODEX_API_KEY-<unset>}\"\n"
        "env\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)
    completed = subprocess.run(
        [str(probe)],
        env=child_env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    out = completed.stdout
    assert secret not in out
    assert "OPENAI_API_KEY=<unset>" in out or "OPENAI_API_KEY=\n" in out.replace("\r", "")
    assert "sk-planted" not in out
    # PATH from policy.set must still be visible so allowlisted tools can run.
    assert "PATH=" in out


def _execpolicy_decision(rules: Path, command: list[str]) -> dict:
    proc = subprocess.run(
        [
            "codex",
            "execpolicy",
            "check",
            "--pretty",
            "--resolve-host-executables",
            "--rules",
            str(rules),
            "--",
            *command,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


@requires_codex
def test_execpolicy_allows_readonly_forbids_interpreters(tmp_path: Path) -> None:
    home = prepare_codex_home(tmp_path / "codex-home", sandbox_env("sk-unused"))
    rules = rules_path(home)

    allowed = [
        ["rg", "--version"],
        ["cat", "README.md"],
        ["ls", "-la"],
        ["head", "-n", "5", "README.md"],
        ["sed", "-n", "1p", "README.md"],
        ["git", "diff", "HEAD"],
        ["git", "log", "-1"],
        ["git", "show", "HEAD"],
        ["git", "status"],
    ]
    for command in allowed:
        payload = _execpolicy_decision(rules, command)
        assert payload.get("decision") == "allow", command

    forbidden = [
        ["python3", "-c", "print(1)"],
        ["python", "-c", "print(1)"],
        ["node", "-e", "1"],
        ["npm", "test"],
        ["curl", "https://example.com"],
        ["bash", "-lc", "echo hi"],
        ["sh", "-c", "echo hi"],
        ["pip", "install", "requests"],
        ["make", "test"],
        ["cargo", "test"],
        ["pytest"],
        ["docker", "ps"],
    ]
    for command in forbidden:
        payload = _execpolicy_decision(rules, command)
        assert payload.get("decision") == "forbidden", (command, payload)
