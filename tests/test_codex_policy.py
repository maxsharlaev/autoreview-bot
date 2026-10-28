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
    assert policy["inherit"] == "core"
    assert policy["ignore_default_excludes"] is False
    assert "PATH" not in policy.get("set", {})
    assert "HOME" not in policy.get("set", {})
    assert "OPENAI_API_KEY" not in policy["set"]
    assert "CODEX_API_KEY" not in policy["set"]
    assert (home / "rules" / RULES_FILENAME).is_file()
    rules = (home / "rules" / RULES_FILENAME).read_text(encoding="utf-8")
    assert 'pattern = ["rg"]' in rules
    assert 'pattern = ["rg", "--pre"]' in rules
    assert 'pattern = ["sed", "-n"]' not in rules
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


def test_planted_program_run_under_policy_cannot_see_model_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        'echo "OPENAI_API_KEY=${OPENAI_API_KEY-<unset>}"\n'
        'echo "CODEX_API_KEY=${CODEX_API_KEY-<unset>}"\n'
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


@requires_codex
@pytest.mark.parametrize(
    "command",
    [
        ["rg", "--pre", "hook", "pattern"],
        ["rg", "--pre-glob", "*.sh", "pattern"],
        ["sed", "-e", "s/a/b/"],
        ["sed", "--expression", "s/a/b/"],
        ["find", ".", "-exec", "true", ";"],
        ["find", ".", "-name", "*.py"],
        ["git", "-c", "alias.x=!true", "x"],
        ["git", "--config", "alias.x=!true", "x"],
    ],
)
def test_execpolicy_forbids_known_bypass_prefixes(tmp_path: Path, command: list[str]) -> None:
    home = prepare_codex_home(tmp_path / "codex-home", sandbox_env("sk-unused"))
    payload = _execpolicy_decision(rules_path(home), command)
    assert payload.get("decision") == "forbidden", (command, payload)


@requires_codex
def test_execpolicy_sed_is_not_allowlisted(tmp_path: Path) -> None:
    """sed is not on the allowlist; a plain sed invocation must not decide allow."""
    home = prepare_codex_home(tmp_path / "codex-home", sandbox_env("sk-unused"))
    payload = _execpolicy_decision(rules_path(home), ["sed", "-n", "1p", "README.md"])
    assert payload.get("decision") != "allow"


@requires_codex
@pytest.mark.skipif(
    __import__("sys").platform != "linux",
    reason="Codex linux sandbox + /proc isolation is Linux-only",
)
def test_sandbox_proc_environ_isolation_hides_canary_and_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside the runner-equivalent sandbox, /proc/*/environ must not expose a
    planted worker canary or the model key names (values never asserted).
    """
    import json
    import shutil
    import subprocess
    import sys
    import tempfile

    canary_name = "OPR_ISOLATION_CANARY"
    monkeypatch.setenv(canary_name, "canary-value-do-not-print")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "planted-worker-secret-not-for-codex")
    secret = "sk-proc-isolation-probe-dummy-key"
    parent = sandbox_env(secret)
    assert canary_name not in parent
    assert "GITHUB_APP_PRIVATE_KEY" not in parent

    # Codex refuses PATH aliases when CODEX_HOME is under /tmp.
    root = Path("/var/tmp") if Path("/var/tmp").is_dir() else Path(tempfile.gettempdir())
    if str(root.resolve()).startswith("/tmp"):
        pytest.skip("Codex sandbox helpers cannot be created under /tmp")
    home = Path(tempfile.mkdtemp(prefix="opr-codex-home-test-", dir=str(root)))
    try:
        prepare_codex_home(home, parent)
        env = sandbox_env(secret, codex_home=home)
        assert canary_name not in env

        probe = tmp_path / "proc_probe.py"
        probe.write_text(
            "\n".join(
                [
                    "import glob, os, json",
                    "hits_key, hits_canary, hits_gapp = [], [], []",
                    "for path in glob.glob('/proc/[0-9]*/environ'):",
                    "    try:",
                    "        data = open(path, 'rb').read()",
                    "    except OSError:",
                    "        continue",
                    "    pid = path.split('/')[2]",
                    "    if b'OPENAI_API_KEY=' in data or b'CODEX_API_KEY=' in data:",
                    "        hits_key.append(pid)",
                    "    if b'OPR_ISOLATION_CANARY=' in data:",
                    "        hits_canary.append(pid)",
                    "    if b'GITHUB_APP_PRIVATE_KEY=' in data:",
                    "        hits_gapp.append(pid)",
                    "print(json.dumps({",
                    "  'self_has_key': 'OPENAI_API_KEY' in os.environ or 'CODEX_API_KEY' in os.environ,",
                    "  'self_has_canary': 'OPR_ISOLATION_CANARY' in os.environ,",
                    "  'pids_with_key': hits_key,",
                    "  'pids_with_canary': hits_canary,",
                    "  'pids_with_gapp': hits_gapp,",
                    "  'proc_count': len(glob.glob('/proc/[0-9]*')),",
                    "}))",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            ["codex", "sandbox", "--", sys.executable, str(probe)],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").lower()
            if "namespace" in err or "userns" in err or "codex-linux-sandbox" in err:
                pytest.skip(f"linux sandbox unavailable in this environment: {completed.stderr[:200]}")
            pytest.fail(f"sandbox probe failed rc={completed.returncode}: {completed.stderr[:400]}")
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        assert payload["self_has_canary"] is False
        assert payload["pids_with_canary"] == []
        assert payload["pids_with_gapp"] == []
        assert payload["self_has_key"] is False
        assert payload["pids_with_key"] == []
        assert payload["proc_count"] < 20
    finally:
        shutil.rmtree(home, ignore_errors=True)
