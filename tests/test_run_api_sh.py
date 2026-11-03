"""scripts/run-api.sh uvicorn flag selection."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run-api.sh"

requires_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="sh not available")


def _run(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uvicorn = fake_bin / "uvicorn"
    uvicorn.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    uvicorn.chmod(uvicorn.stat().st_mode | stat.S_IEXEC)
    full_env = {**os.environ, **env, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"}
    return subprocess.run(
        ["sh", str(SCRIPT)],
        cwd=str(REPO),
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


@requires_sh
def test_run_api_default_no_proxy_headers(tmp_path: Path) -> None:
    proc = _run(tmp_path, {"UVICORN_PROXY_HEADERS": "0"})
    assert proc.returncode == 0, proc.stderr
    args = proc.stdout.strip().splitlines()
    assert "--no-proxy-headers" in args
    assert "--proxy-headers" not in args


@requires_sh
def test_run_api_proxy_headers_requires_allow_ips(tmp_path: Path) -> None:
    proc = _run(tmp_path, {"UVICORN_PROXY_HEADERS": "1", "UVICORN_FORWARDED_ALLOW_IPS": ""})
    assert proc.returncode != 0


@requires_sh
def test_run_api_proxy_headers_enabled(tmp_path: Path) -> None:
    proc = _run(
        tmp_path,
        {"UVICORN_PROXY_HEADERS": "1", "UVICORN_FORWARDED_ALLOW_IPS": "10.0.0.1"},
    )
    assert proc.returncode == 0, proc.stderr
    args = proc.stdout.strip().splitlines()
    assert "--proxy-headers" in args
    assert "--no-proxy-headers" not in args
    assert any(a.startswith("--forwarded-allow-ips=10.0.0.1") for a in args)
