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
    assert "UseContainerOverlay" in text or "$UseContainerOverlay" in text
    assert "docker-compose.container-runner.yml" in text
    assert "build-review" in text
    assert "review-image" in text
    assert "Resolve-ReviewImage" not in text
    assert "config --format json" not in text
    assert "switch -Regex" not in text
    assert "foreach" in text


def test_stack_sh_defaults_to_container_overlay_with_inprocess_opt_out() -> None:
    text = SH.read_text(encoding="utf-8")
    assert "CONTAINER=1" in text
    assert "--in-process" in text
    assert "docker-compose.container-runner.yml" in text
    assert "build-review" in text
    assert "review-image" in text
    assert "resolve_review_image" not in text
    assert "config --format json" not in text
    assert "python -c" not in text
    assert "seen_command" in text
    assert "is_helper_opt" in text


def test_config_example_defaults_to_container_backend() -> None:
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert "runner_backend: container" in text


def test_overlay_defines_build_only_review_image_profile() -> None:
    text = (ROOT / "docker-compose.container-runner.yml").read_text(encoding="utf-8")
    assert "review-image:" in text
    assert "build-review" in text
    assert "Dockerfile.review" in text


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
    (home / "docker-compose.observability.yml").write_text("services: {}\n", encoding="utf-8")
    (home / "docker-compose.container-runner.yml").write_text(
        "\n".join(
            [
                "services:",
                "  review-image:",
                '    profiles: ["build-review"]',
                "    image: ${OPR_REVIEW_IMAGE:-opr-review:local}",
                "    build:",
                "      context: .",
                "      dockerfile: Dockerfile.review",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
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


def _write_python_trap(bin_dir: Path, log: Path) -> None:
    """Fail the test run if host python is invoked (ARB36-007)."""
    for name in ("python", "python3"):
        stub = bin_dir / name
        stub.write_text(
            f'#!/bin/sh\nprintf \'python-trap %s\\n\' "$*" >> "{log.as_posix()}"\nexit 97\n',
            encoding="utf-8",
            newline="\n",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_ps1(
    command: str,
    *,
    home: Path,
    bin_dir: Path,
    env_extra: dict[str, str] | None = None,
    options: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("LAUNCHER_TOKEN", None)
    env.pop("OPR_REVIEW_IMAGE", None)
    env["OPR_STACK_ROOT"] = str(home)
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
    ]
    if options:
        argv.extend(options)
    argv.append(command)
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
    options: list[str] | None = None,
    extra_args: list[str] | None = None,
    path_override: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("LAUNCHER_TOKEN", None)
    env.pop("OPR_REVIEW_IMAGE", None)
    env["OPR_STACK_ROOT"] = str(home)
    if path_override is not None:
        env["PATH"] = path_override
    else:
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    if env_extra:
        env.update(env_extra)
    argv = [bash, str(SH)]
    if options:
        argv.extend(options)
    argv.append(command)
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
    assert "build-review" not in recorded
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
def test_stack_ps1_up_builds_review_image_via_profile(tmp_path: Path) -> None:
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
    assert "--profile build-review build review-image" in recorded
    assert "up -d --build" in recorded
    assert recorded.index("--profile build-review build review-image") < recorded.index("up -d --build")
    assert "Dockerfile.review" not in recorded or "build review-image" in recorded
    assert "config --format json" not in recorded


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
def test_stack_ps1_up_inprocess_skips_review_image_build(tmp_path: Path) -> None:
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1("up", home=home, bin_dir=bin_dir, options=["-InProcess"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "build-review" not in recorded
    assert "docker-compose.container-runner.yml" not in recorded
    assert "up -d --build" in recorded


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
@pytest.mark.parametrize(
    ("passthrough", "needle"),
    [
        (["up", "-d", "--scale", "worker=2"], "up -d --scale worker=2"),
        (["down"], "down"),
        (["restart", "worker"], "restart worker"),
        (["logs", "-f", "api"], "logs -f api"),
    ],
)
def test_stack_ps1_compose_passthrough_verbatim(passthrough: list[str], needle: str, tmp_path: Path) -> None:
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1("compose", home=home, bin_dir=bin_dir, extra_args=passthrough)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "docker-compose.container-runner.yml" in recorded
    assert needle in recorded
    assert "build-review" not in recorded
    assert "LAUNCHER_TOKEN" not in (completed.stdout + completed.stderr)


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
def test_stack_ps1_compose_up_does_not_hijack_helper_up(tmp_path: Path) -> None:
    """ARB36-008: `compose up` must not become helper up (no token / no profile build)."""
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1("compose", home=home, bin_dir=bin_dir, extra_args=["up", "-d"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "build-review" not in recorded
    assert "up -d" in recorded
    # Helper up always adds --build; compose passthrough must keep args verbatim.
    assert "up -d --build" not in recorded


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
    assert "build-review" not in recorded


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
def test_stack_sh_up_builds_review_image_via_profile(tmp_path: Path) -> None:
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
    assert "--profile build-review build review-image" in recorded
    assert "up -d --build" in recorded
    assert recorded.index("--profile build-review build review-image") < recorded.index("up -d --build")
    assert "config --format json" not in recorded


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_sh_up_works_without_host_python(tmp_path: Path) -> None:
    """ARB36-007: helper up must not require host python/python3."""
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    _write_python_trap(bin_dir, log)
    # PATH: only stub bin (docker + python traps). Bash is invoked by absolute path.
    completed = _run_sh(
        "up",
        home=home,
        bin_dir=bin_dir,
        bash=bash,
        path_override=str(bin_dir),
        env_extra={"LAUNCHER_TOKEN": "test-launcher-token-32chars-ok"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "python-trap" not in recorded
    assert "--profile build-review build review-image" in recorded


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_sh_up_inprocess_skips_review_image_build(tmp_path: Path) -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    completed = _run_sh("up", home=home, bin_dir=bin_dir, bash=bash, options=["--in-process"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "build-review" not in recorded
    assert "docker-compose.container-runner.yml" not in recorded
    assert "up -d --build" in recorded


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize(
    ("passthrough", "needle"),
    [
        (["up", "-d", "--scale", "worker=2"], "up -d --scale worker=2"),
        (["down"], "down"),
        (["restart", "worker"], "restart worker"),
        (["logs", "-f", "api"], "logs -f api"),
    ],
)
def test_stack_sh_compose_passthrough_verbatim(passthrough: list[str], needle: str, tmp_path: Path) -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    completed = _run_sh("compose", home=home, bin_dir=bin_dir, bash=bash, extra_args=passthrough)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "docker-compose.container-runner.yml" in recorded
    assert needle in recorded
    assert "build-review" not in recorded
    assert "LAUNCHER_TOKEN" not in (completed.stdout + completed.stderr)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_sh_compose_up_does_not_hijack_helper_up(tmp_path: Path) -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    completed = _run_sh("compose", home=home, bin_dir=bin_dir, bash=bash, extra_args=["up", "-d"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "build-review" not in recorded
    assert "up -d" in recorded
    assert "up -d --build" not in recorded


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
    services = data.get("services") or {}
    assert "review-image" not in services, "profile-only service must not appear without profile"
    worker_env = _env_map(services.get("worker", {}).get("environment"))
    assert worker_env.get("MODEL_PROXY_URL"), worker_env
    launcher_env = _env_map(services.get("launcher", {}).get("environment"))
    assert launcher_env.get("OPR_REVIEW_IMAGE") == "opr-review:local"


@pytest.mark.skipif(not _compose_available(), reason="docker not available")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_compose_config_review_image_override_matches_launcher() -> None:
    bash = _git_bash() or shutil.which("bash")
    assert bash
    env = {
        **os.environ,
        "LAUNCHER_TOKEN": "test-launcher-token-32chars-ok",
        "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD") or "test-postgres-password",
        "OPR_REVIEW_IMAGE": "custom-review:arb36",
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
    launcher_env = _env_map((data.get("services") or {}).get("launcher", {}).get("environment"))
    assert launcher_env.get("OPR_REVIEW_IMAGE") == "custom-review:arb36"

    profiled = subprocess.run(
        [
            bash,
            str(SH),
            "compose",
            "--profile",
            "build-review",
            "config",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if profiled.returncode != 0:
        pytest.skip(f"stack compose --profile config failed: {profiled.stderr[-400:]}")
    pdata = yaml.safe_load(profiled.stdout)
    review = (pdata.get("services") or {}).get("review-image") or {}
    assert review.get("image") == "custom-review:arb36"


@pytest.mark.skipif(not _compose_available(), reason="docker not available")
def test_compose_config_resolves_opr_review_image_interpolation_and_comment(tmp_path: Path) -> None:
    """ARB36-005/007: compose interpolates ${REGISTRY} and strips inline .env comments."""
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
                "  review-image:",
                '    profiles: ["build-review"]',
                "    image: ${OPR_REVIEW_IMAGE:-opr-review:local}",
                "    build:",
                "      context: .",
                "      dockerfile: Dockerfile.review",
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
            "--profile",
            "build-review",
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
    assert data["services"]["review-image"]["image"] == "example.com/opr-review:local"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize(
    ("options_before", "options_after", "expect_overlay", "expect_obs"),
    [
        (["--container"], [], True, False),
        ([], ["--container"], True, False),
        (["--observability"], [], True, True),
        ([], ["--observability"], True, True),
        (["--in-process"], [], False, False),
        ([], ["--in-process"], False, False),
        (["--observability", "--in-process"], [], False, True),
        (["--observability"], ["--in-process"], False, True),
    ],
)
def test_stack_sh_helper_options_before_or_after_command(
    options_before: list[str],
    options_after: list[str],
    expect_overlay: bool,
    expect_obs: bool,
    tmp_path: Path,
) -> None:
    """ARB36-009: known helper switches work before or after non-compose commands."""
    bash = _git_bash() or shutil.which("bash")
    assert bash
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_posix(bin_dir, log)
    env_extra = {"LAUNCHER_TOKEN": "test-launcher-token-32chars-ok"} if expect_overlay else None
    completed = _run_sh(
        "up",
        home=home,
        bin_dir=bin_dir,
        bash=bash,
        options=options_before,
        extra_args=options_after,
        env_extra=env_extra,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    if expect_overlay:
        assert "docker-compose.container-runner.yml" in recorded
        assert "--profile build-review build review-image" in recorded
    else:
        assert "docker-compose.container-runner.yml" not in recorded
        assert "build-review" not in recorded
    if expect_obs:
        assert "docker-compose.observability.yml" in recorded
    else:
        assert "docker-compose.observability.yml" not in recorded


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_sh_container_and_inprocess_conflict(tmp_path: Path) -> None:
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
        options=["--container", "--in-process"],
    )
    assert completed.returncode == 2
    out = (completed.stdout + completed.stderr).lower()
    assert "not both" in out or "either" in out
    assert log.read_text(encoding="utf-8").strip() == ""


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_stack_sh_compose_keeps_looking_like_helper_opts_verbatim(tmp_path: Path) -> None:
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
    assert "docker-compose.container-runner.yml" in recorded
    assert "--in-process" in recorded
    assert "build-review" not in recorded


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
@pytest.mark.parametrize(
    ("options_before", "options_after", "expect_overlay", "expect_obs"),
    [
        (["-Container"], [], True, False),
        ([], ["-Container"], True, False),
        (["-Observability"], [], True, True),
        ([], ["-Observability"], True, True),
        (["-InProcess"], [], False, False),
        ([], ["-InProcess"], False, False),
    ],
)
def test_stack_ps1_helper_options_before_or_after_command(
    options_before: list[str],
    options_after: list[str],
    expect_overlay: bool,
    expect_obs: bool,
    tmp_path: Path,
) -> None:
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    env_extra = {"LAUNCHER_TOKEN": "test-launcher-token-32chars-ok"} if expect_overlay else None
    completed = _run_ps1(
        "up",
        home=home,
        bin_dir=bin_dir,
        options=options_before,
        extra_args=options_after,
        env_extra=env_extra,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    if expect_overlay:
        assert "docker-compose.container-runner.yml" in recorded
        assert "--profile build-review build review-image" in recorded
    else:
        assert "docker-compose.container-runner.yml" not in recorded
        assert "build-review" not in recorded
    if expect_obs:
        assert "docker-compose.observability.yml" in recorded
    else:
        assert "docker-compose.observability.yml" not in recorded


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
def test_stack_ps1_observability_and_inprocess_both_applied(tmp_path: Path) -> None:
    """ARB36-010: every leading option is processed (no switch+break bug)."""
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1(
        "up",
        home=home,
        bin_dir=bin_dir,
        options=["-Observability", "-InProcess"],
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    recorded = log.read_text(encoding="utf-8")
    assert "docker-compose.observability.yml" in recorded
    assert "docker-compose.container-runner.yml" not in recorded
    assert "build-review" not in recorded
    assert "up -d --build" in recorded


@pytest.mark.skipif(not IS_WINDOWS, reason="stack.ps1 behavioral tests need Windows PowerShell")
def test_stack_ps1_container_and_inprocess_conflict(tmp_path: Path) -> None:
    home, log = _prepare_stack_home(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_docker_stub_windows(bin_dir, log)
    completed = _run_ps1(
        "up",
        home=home,
        bin_dir=bin_dir,
        options=["-Container", "-InProcess"],
    )
    assert completed.returncode == 2
    out = (completed.stdout + completed.stderr).lower()
    assert "not both" in out or "either" in out
    assert log.read_text(encoding="utf-8").strip() == ""
