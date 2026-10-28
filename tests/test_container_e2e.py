"""Opt-in end-to-end: worker staging -> launcher -> review via volume Subpath.

Requires OPR_CONTAINER_E2E=1 and Docker. Uses a stub review image (no model key /
no secrets printed). Validates ARB-27-001 volume Subpath mounts through the real
launcher create path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

docker = shutil.which("docker")
enabled = os.environ.get("OPR_CONTAINER_E2E", "").strip().lower() in {"1", "true", "yes"}
pytestmark = pytest.mark.skipif(not enabled or not docker, reason="Set OPR_CONTAINER_E2E=1 with Docker available")

ROOT = Path(__file__).resolve().parents[1]
VOLUME = "opr_checkouts"
NETWORK = "autoreview_review_egress"
STUB_IMAGE = "opr-review:e2e-stub"
LAUNCHER_IMAGE = "opr-launcher:e2e"
SECCOMP = ROOT / "docker" / "review" / "seccomp" / "review-bwrap.json"


def _run(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=True, text=True)


@pytest.fixture(scope="module")
def e2e_stack():
    token = "e2e-launcher-token-32chars-ok!!"
    # Volume + network (named, matching compose overlay).
    _run([docker, "volume", "create", VOLUME])
    _run([docker, "network", "create", NETWORK])

    stub_dir = ROOT / ".e2e-stub"
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "Dockerfile").write_text(
        "FROM busybox:1.36\n"
        "COPY entrypoint.sh /entrypoint.sh\n"
        "RUN chmod 755 /entrypoint.sh\n"
        "USER 1000:1000\n"
        "ENTRYPOINT [\"/entrypoint.sh\"]\n",
        encoding="utf-8",
        newline="\n",
    )
    (stub_dir / "entrypoint.sh").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "test -f /checkout/ok.py\n"
        "test -f /job/prompt.txt\n"
        "test -f /job/schema.json\n"
        "mkdir -p /work\n"
        "printf '%s' '{\"findings\":[],\"e2e\":true}' > /work/out.json\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    built = _run([docker, "build", "-t", STUB_IMAGE, str(stub_dir)])
    assert built.returncode == 0, built.stderr[-800:]

    launcher_build = _run(
        [docker, "build", "-f", str(ROOT / "Dockerfile.launcher"), "-t", LAUNCHER_IMAGE, str(ROOT)]
    )
    assert launcher_build.returncode == 0, launcher_build.stderr[-800:]

    name = f"opr-launcher-e2e-{uuid.uuid4().hex[:8]}"
    run = _run(
        [
            docker,
            "run",
            "-d",
            "--name",
            name,
            "--network",
            NETWORK,
            "-e",
            f"LAUNCHER_TOKEN={token}",
            "-e",
            "OPR_CHECKOUT_BASE=/var/opr/checkouts",
            "-e",
            f"OPR_CHECKOUT_VOLUME={VOLUME}",
            "-e",
            f"OPR_REVIEW_IMAGE={STUB_IMAGE}",
            "-e",
            f"OPR_REVIEW_NETWORK={NETWORK}",
            "-e",
            "DOCKER_HOST=unix:///var/run/docker.sock",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{VOLUME}:/var/opr/checkouts",
            "-p",
            "18080:8080",
            LAUNCHER_IMAGE,
        ]
    )
    assert run.returncode == 0, run.stderr[-500:]
    # Wait for health
    deadline = time.time() + 60
    healthy = False
    while time.time() < deadline:
        try:
            r = httpx.get("http://127.0.0.1:18080/healthz", timeout=2.0)
            if r.status_code == 200:
                healthy = True
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    if not healthy:
        logs = _run([docker, "logs", name]).stdout + _run([docker, "logs", name]).stderr
        _run([docker, "rm", "-f", name])
        pytest.fail(f"launcher did not become healthy; logs_tail={logs[-1000:]}")

    yield {"token": token, "name": name, "url": "http://127.0.0.1:18080"}

    _run([docker, "rm", "-f", name])


@pytest.mark.asyncio
async def test_e2e_volume_subpath_staging_to_output(e2e_stack) -> None:
    token = e2e_stack["token"]
    # Stage onto the named volume via a helper container (simulates worker volume mount).
    job_id = uuid.uuid4().hex
    helper = _run(
        [
            docker,
            "run",
            "--rm",
            "-v",
            f"{VOLUME}:/var/opr/checkouts",
            "busybox:1.36",
            "sh",
            "-c",
            (
                f"mkdir -p /var/opr/checkouts/{job_id}/repo && "
                f"echo 'print(1)' > /var/opr/checkouts/{job_id}/repo/ok.py && "
                f"echo prompt > /var/opr/checkouts/{job_id}/prompt.txt && "
                f"echo '{{}}' > /var/opr/checkouts/{job_id}/schema.json && "
                f"echo '{{\"model\":\"stub\",\"timeout_seconds\":30}}' > /var/opr/checkouts/{job_id}/job.json"
            ),
        ]
    )
    assert helper.returncode == 0, helper.stderr

    # Call launcher like the worker does (without going through ContainerReviewExecutor staging,
    # because the shared volume lives in Docker, not on the Windows host path).
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Model-Api-Key": "sk-e2e-not-a-real-secret",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{e2e_stack['url']}/v1/reviews",
            headers=headers,
            json={"job_id": job_id, "checkout_ref": f"{job_id}/repo"},
        )
    assert response.status_code == 200, response.text[:500]
    data = response.json()
    assert data["status"] == "ok"
    output = json.loads(data["output"])
    assert output.get("e2e") is True
    # Never echo secrets in assertions beyond fixed test tokens (no env dumps).
