"""M4: stack helpers default to container-runner overlay with in-process opt-out."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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
    assert '"compose"' in text or "'compose'" in text


def test_stack_sh_defaults_to_container_overlay_with_inprocess_opt_out() -> None:
    text = SH.read_text(encoding="utf-8")
    assert "CONTAINER=1" in text
    assert "--in-process" in text
    assert "docker-compose.container-runner.yml" in text
    assert '"$COMMAND" == "up"' in text
    assert "compose)" in text
    assert "config --format json" in text
    assert "dotenv_get OPR_REVIEW_IMAGE" not in text


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
    # Logs argv; for `compose config --format json` prints launcher image JSON (ARB36-005).
    stub.write_text(
        "\r\n".join(
            [
                "@echo off",
                f'>>"{log}" echo %*',
                'echo %*| findstr /I /C:"config" >nul',
                "if errorlevel 1 goto :done",
                'echo %*| findstr /I /C:"json" >nul',
                "if errorlevel 1 goto :done",
                "if not defined OPR_STUB_REVIEW_IMAGE set OPR_STUB_REVIEW_IMAGE=opr-review:local",
                ('echo {"services":{"launcher":{"environment":{"OPR_REVIEW_IMAGE":"%OPR_STUB_REVIEW_IMAGE%"}}}}'),
                ":done",
                "exit /b 0",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\r\n",
    )


def _write_docker_stub_posix(bin_dir: Path, log: Path) -> Path:
    stub = bin_dir / "docker"
    log_posix = log.as_posix()
    stub.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f'printf \'%s\\n\' "$*" >> "{log_posix}"',
                'case "$*" in',
                "  *config*json*|*config*--format*)",
                '    img="${OPR_STUB_REVIEW_IMAGE:-opr-review:local}"',
                (
                    "    printf '%s\\n' "
                    '"{\\"services\\":{\\"launcher\\":{\\"environment\\":'
                    '{\\"OPR_REVIEW_IMAGE\\":\\"$img\\"}}}}"'
                ),
                "    ;;",
                "esac",
                "exit 0",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _run_ps1(
    command: str,
    *,
    home: Path,
    bin_dir: Path,
    env_extra: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("LAUNCHER_TOKEN", None)
    env.pop("OPR_REVIEW_IMAGE", None)
    env["OPR_STACK_ROOT"] = str(home)
    # Prefer our docker.cmd stub over Docker Desktop's docker.exe (PATHEXT order).
    env["PATHEXT"] = ".CMD;.BAT;.COM;.EXE" + os.pathsep + env.get("PATHEXT", "")
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    if env_extra:
        env.update(env_extra)
    argv = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(PS1),
        command,
    ]
    if extra_args:
        argv.extend(extra_args)
    return subprocess.run(
        argv,
        cwd=str(home),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _run_sh(
    command: str,
    *,
    home: Path,
    bin_dir: Path,
    bash: str,
    env_extra: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("LAUNCHER_TOKEN", None)
    env.pop("OPR_REVIEW_IMAGE", None)
    env["OPR_STACK_ROOT"] = str(home)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    if env_extra:
        env.update(env_extra)
    argv = [bash, str(SH), command]
    if extra_args:
        argv.extend(extra_args)
    return subprocess.run(
        argv,
        cwd=str(home),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
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
    assert "config --format json" in recorded
    assert "build -f Dockerfile.review -t opr-review:local ." in recorded
    assert recorded.index("config --format json") < recorded.index("build -f Dockerfile.review")
    assert recorded.index("build -f Dockerfile.review") < recorded.index("up -d --build")


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
    assert "config --format json" in recorded
    assert "build -f Dockerfile.review -t opr-review:local ." in recorded
    assert recorded.index("config --format json") < recorded.index("build -f Dockerfile.review")
    assert recorded.index("build -f Dockerfile.review") < recorded.index("up -d --build")


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
def test_stack_ps1_up_inprocess_skips_review_image_build(tmp_path: Path) -> None:
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1("up", home=home, bin_dir=bin_dir, extra_args=["-InProcess"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "Dockerfile.review" not in recorded
    assert "config --format json" not in recorded
    assert "docker-compose.container-runner.yml" not in recorded
    assert "up -d --build" in recorded


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
@pytest.mark.parametrize("command", ["status", "down", "restart", "logs"])
def test_stack_ps1_lifecycle_skips_review_image_build(command: str, tmp_path: Path) -> None:
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1(command, home=home, bin_dir=bin_dir)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "Dockerfile.review" not in recorded
    assert "config --format json" not in recorded


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
def test_stack_ps1_up_uses_compose_config_image_not_dotenv(tmp_path: Path) -> None:
    home, log = _prepare_stack_home(tmp_path)
    (home / ".env").write_text(
        "OPR_REVIEW_IMAGE=wrong-from-dotenv:tag # should be ignored by helper\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1(
        "up",
        home=home,
        bin_dir=bin_dir,
        env_extra={
            "LAUNCHER_TOKEN": "test-launcher-token-32chars-ok",
            "OPR_STUB_REVIEW_IMAGE": "from-compose:tag",
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "build -f Dockerfile.review -t from-compose:tag ." in recorded
    assert "wrong-from-dotenv" not in recorded


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
def test_stack_ps1_compose_passthrough_uses_overlay_without_build(tmp_path: Path) -> None:
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1("compose", home=home, bin_dir=bin_dir, extra_args=["config"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "docker-compose.container-runner.yml" in recorded
    assert " config" in recorded or recorded.strip().endswith("config")
    assert "Dockerfile.review" not in recorded
    assert "up -d --build" not in recorded


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
def test_stack_ps1_compose_inprocess_omits_overlay(tmp_path: Path) -> None:
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1("compose", home=home, bin_dir=bin_dir, extra_args=["-InProcess", "config"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "docker-compose.container-runner.yml" not in recorded
    assert "docker-compose.yml" in recorded


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_sh_up_inprocess_skips_review_image_build(tmp_path: Path) -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    completed = _run_sh("up", home=home, bin_dir=bin_dir, bash=bash, extra_args=["--in-process"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "Dockerfile.review" not in recorded
    assert "config --format json" not in recorded
    assert "docker-compose.container-runner.yml" not in recorded
    assert "up -d --build" in recorded


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("command", ["status", "down", "restart", "logs"])
def test_stack_sh_lifecycle_skips_review_image_build(command: str, tmp_path: Path) -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    completed = _run_sh(command, home=home, bin_dir=bin_dir, bash=bash)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "Dockerfile.review" not in recorded
    assert "config --format json" not in recorded


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_sh_up_uses_compose_config_image_not_dotenv(tmp_path: Path) -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    (home / ".env").write_text(
        "OPR_REVIEW_IMAGE=wrong-from-dotenv:tag # should be ignored by helper\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    completed = _run_sh(
        "up",
        home=home,
        bin_dir=bin_dir,
        bash=bash,
        env_extra={
            "LAUNCHER_TOKEN": "test-launcher-token-32chars-ok",
            "OPR_STUB_REVIEW_IMAGE": "from-compose:tag",
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "build -f Dockerfile.review -t from-compose:tag ." in recorded
    assert "wrong-from-dotenv" not in recorded


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_sh_compose_passthrough_uses_overlay_without_build(tmp_path: Path) -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    completed = _run_sh("compose", home=home, bin_dir=bin_dir, bash=bash, extra_args=["config"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "docker-compose.container-runner.yml" in recorded
    assert "config" in recorded
    assert "Dockerfile.review" not in recorded
    assert "up -d --build" not in recorded


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_sh_compose_inprocess_omits_overlay(tmp_path: Path) -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    completed = _run_sh(
        "compose",
        home=home,
        bin_dir=bin_dir,
        bash=bash,
        extra_args=["--in-process", "config"],
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "docker-compose.container-runner.yml" not in recorded
    assert "docker-compose.yml" in recorded


def _compose_available() -> bool:
    return shutil.which("docker") is not None


def _env_map(svc_env: object) -> dict[str, str]:
    if isinstance(svc_env, dict):
        return {str(k): str(v) for k, v in svc_env.items()}
    if isinstance(svc_env, list):
        out: dict[str, str] = {}
        for item in svc_env:
            text = str(item)
            if "=" in text:
                k, v = text.split("=", 1)
                out[k] = v
        return out
    return {}


@pytest.mark.skipif(not _compose_available(), reason="docker not available")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_compose_config_worker_has_model_proxy_url() -> None:
    """ARB36-004: stack compose config with overlay exposes worker MODEL_PROXY_URL."""
    bash = _git_bash() or shutil.which("bash")
    assert bash
    env = {
        **os.environ,
        "LAUNCHER_TOKEN": os.environ.get("LAUNCHER_TOKEN") or "test-launcher-token-32chars-ok",
        "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD") or "test-postgres-password",
    }
    completed = subprocess.run(
        [bash, str(SH), "compose", "config"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        pytest.skip(f"stack compose config failed: {completed.stderr[-400:]}")
    data = yaml.safe_load(completed.stdout)
    worker_env = _env_map((data.get("services") or {}).get("worker", {}).get("environment"))
    assert worker_env.get("MODEL_PROXY_URL"), worker_env


@pytest.mark.skipif(not _compose_available(), reason="docker not available")
def test_compose_config_resolves_opr_review_image_interpolation_and_comment(tmp_path: Path) -> None:
    """ARB36-005: compose interpolates ${REGISTRY} and strips inline .env comments."""
    home = tmp_path / "compose-home"
    home.mkdir()
    (home / "docker-compose.yml").write_text(
        "services:\n  placeholder:\n    image: alpine:3.20\n",
        encoding="utf-8",
    )
    (home / "docker-compose.container-runner.yml").write_text(
        "\n".join(
            [
                "services:",
                "  launcher:",
                "    image: alpine:3.20",
                "    environment:",
                '      OPR_REVIEW_IMAGE: "${OPR_REVIEW_IMAGE:-opr-review:local}"',
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    (home / ".env").write_text(
        "OPR_REVIEW_IMAGE=${REGISTRY}/opr-review:local # inline comment\n",
        encoding="utf-8",
        newline="\n",
    )
    env = {**os.environ, "REGISTRY": "example.com"}
    env.pop("OPR_REVIEW_IMAGE", None)
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.container-runner.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=str(home),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if rendered.returncode != 0:
        pytest.skip(f"docker compose config failed: {rendered.stderr[-400:]}")
    data = json.loads(rendered.stdout)
    launcher_env = _env_map(data["services"]["launcher"]["environment"])
    assert launcher_env.get("OPR_REVIEW_IMAGE") == "example.com/opr-review:local"
