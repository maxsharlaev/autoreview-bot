"""M4: stack helpers default to container-runner overlay with in-process opt-out."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "scripts" / "stack.ps1"
SH = ROOT / "scripts" / "stack.sh"
IS_WINDOWS = sys.platform.startswith("win")


def test_stack_ps1_defaults_to_container_overlay_with_inprocess_opt_out() -> None:
    text = PS1.read_text(encoding="utf-8")
    assert "[switch]$InProcess" in text
    assert "$UseContainerOverlay = -not $InProcess" in text
    assert "docker-compose.container-runner.yml" in text
    assert '$Command -eq "up"' in text


def test_stack_sh_defaults_to_container_overlay_with_inprocess_opt_out() -> None:
    text = SH.read_text(encoding="utf-8")
    assert "CONTAINER=1" in text
    assert "--in-process" in text
    assert "docker-compose.container-runner.yml" in text
    assert '"$COMMAND" == "up"' in text


def test_config_example_defaults_to_container_backend() -> None:
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert "runner_backend: container" in text


def _git_bash() -> str | None:
    for candidate in (
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    which = shutil.which("bash")
    if which and "Windows\\System32\\bash" not in which.replace("/", "\\"):
        return which
    return which


def _prepare_stack_home(tmp_path: Path) -> tuple[Path, Path]:
    """Isolated stack root + docker stub log (no LAUNCHER_TOKEN / no .env)."""
    home = tmp_path / "stack-home"
    home.mkdir()
    (home / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (home / "docker-compose.container-runner.yml").write_text("services: {}\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    return home, log


def _write_docker_stub_windows(bin_dir: Path, log: Path) -> None:
    stub = bin_dir / "docker.cmd"
    stub.write_text(
        f'@echo off\r\n>>"{log}" echo %*\r\nexit /b 0\r\n',
        encoding="utf-8",
        newline="\r\n",
    )


def _write_docker_stub_posix(bin_dir: Path, log: Path) -> Path:
    stub = bin_dir / "docker"
    stub.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{log.as_posix()}"\nexit 0\n',
        encoding="utf-8",
        newline="\n",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _run_ps1(
    command: str, *, home: Path, bin_dir: Path, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("LAUNCHER_TOKEN", None)
    env["OPR_STACK_ROOT"] = str(home)
    # Prefer our docker.cmd stub over Docker Desktop's docker.exe (PATHEXT order).
    env["PATHEXT"] = ".CMD;.BAT;.COM;.EXE" + os.pathsep + env.get("PATHEXT", "")
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PS1),
            command,
        ],
        cwd=str(home),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _run_sh(
    command: str, *, home: Path, bin_dir: Path, bash: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("LAUNCHER_TOKEN", None)
    env["OPR_STACK_ROOT"] = str(home)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [bash, str(SH), command],
        cwd=str(home),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
@pytest.mark.parametrize("command", ["status", "down", "restart", "logs"])
def test_stack_ps1_lifecycle_without_launcher_token_uses_overlay(command: str, tmp_path: Path) -> None:
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1(command, home=home, bin_dir=bin_dir)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "docker-compose.container-runner.yml" in recorded
    assert "LAUNCHER_TOKEN" not in (completed.stdout + completed.stderr)


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
def test_stack_ps1_up_without_launcher_token_fails(tmp_path: Path) -> None:
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1("up", home=home, bin_dir=bin_dir)
    assert completed.returncode == 1
    assert "LAUNCHER_TOKEN" in (completed.stdout + completed.stderr)
    assert log.read_text(encoding="utf-8").strip() == ""


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
def test_stack_ps1_up_with_launcher_token_invokes_compose(tmp_path: Path) -> None:
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1(
        "up",
        home=home,
        bin_dir=bin_dir,
        env_extra={"LAUNCHER_TOKEN": "test-launcher-token-32chars-ok"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "docker-compose.container-runner.yml" in recorded
    assert "up -d --build" in recorded


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("command", ["status", "down", "restart", "logs"])
def test_stack_sh_lifecycle_without_launcher_token_uses_overlay(command: str, tmp_path: Path) -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    completed = _run_sh(command, home=home, bin_dir=bin_dir, bash=bash)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "docker-compose.container-runner.yml" in recorded


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_sh_up_without_launcher_token_fails(tmp_path: Path) -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    completed = _run_sh("up", home=home, bin_dir=bin_dir, bash=bash)
    assert completed.returncode == 1
    assert "LAUNCHER_TOKEN" in (completed.stdout + completed.stderr)
    assert log.read_text(encoding="utf-8").strip() == ""


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_sh_up_with_launcher_token_invokes_compose(tmp_path: Path) -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    completed = _run_sh(
        "up",
        home=home,
        bin_dir=bin_dir,
        bash=bash,
        env_extra={"LAUNCHER_TOKEN": "test-launcher-token-32chars-ok"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "docker-compose.container-runner.yml" in recorded
    assert "up -d --build" in recorded
