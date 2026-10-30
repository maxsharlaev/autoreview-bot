"""Launcher job runner asserts hard-coded Docker create payload."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.launcher.docker_api import DockerApiError
from app.launcher.jobs import LauncherJobError, run_review_job

REPO_SECCOMP = Path(__file__).resolve().parents[1] / "docker" / "review" / "seccomp" / "review-bwrap.json"


class FakeDocker:
    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None
        self.name: str | None = None
        self.started: str | None = None
        self.removed: list[str] = []
        self.wait_code = 0
        self._id = "cid123"

    def create_container(self, name: str, config: dict[str, Any]) -> str:
        self.name = name
        self.created = config
        return self._id

    def start_container(self, container_id: str) -> None:
        self.started = container_id

    def wait_container(self, container_id: str, *, timeout_seconds: int) -> int:
        return self.wait_code

    def logs(self, container_id: str, *, tail: int = 200) -> str:
        return "ok"

    def remove_container(self, container_id: str, *, force: bool = True) -> None:
        self.removed.append(container_id)

    def close(self) -> None:
        return None


@pytest.fixture()
def staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_REVIEW_SECCOMP_PROFILE", str(REPO_SECCOMP))
    monkeypatch.setenv("OPR_CHECKOUT_VOLUME", "opr_checkouts")
    job = tmp_path / "jobdeadbeef"
    repo = job / "repo"
    repo.mkdir(parents=True)
    (job / "prompt.txt").write_text("prompt", encoding="utf-8")
    (job / "schema.json").write_text("{}", encoding="utf-8")
    (job / "job.json").write_text(
        json.dumps(
            {
                "model": "gpt-test",
                "timeout_seconds": 30,
                "reasoning_effort": "low",
                "sandbox": "read-only",
                "approval_policy": "never",
                "allow_unsandboxed": False,
            }
        ),
        encoding="utf-8",
    )
    return tmp_path, "jobdeadbeef", "jobdeadbeef/repo"


def test_run_review_job_create_spec(staged, monkeypatch: pytest.MonkeyPatch) -> None:
    base, job_id, ref = staged
    fake = FakeDocker()

    def write_out(*args, **kwargs):
        # Simulate container writing output during wait.
        out = base / job_id / ".opr-work" / "out.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('{"findings":[]}', encoding="utf-8")
        return 0

    fake.wait_container = lambda container_id, timeout_seconds=0: write_out()  # type: ignore[method-assign]
    result = run_review_job(
        job_id=job_id,
        checkout_ref=ref,
        proxy_token="opr_job_test_token_ok",
        client=fake,  # type: ignore[arg-type]
    )
    assert result["status"] == "ok"
    assert result["output"] == '{"findings":[]}'
    assert fake.created is not None
    host = fake.created["HostConfig"]
    assert "ALL" in host["CapDrop"]
    assert not host.get("Binds")
    mounts = host["Mounts"]
    checkout = next(m for m in mounts if m["Target"] == "/checkout")
    assert checkout["Type"] == "volume"
    assert checkout["ReadOnly"] is True
    assert checkout["VolumeOptions"]["Subpath"] == f"{job_id}/repo"
    assert not any("docker.sock" in str(m).lower() for m in mounts)
    assert host["Privileged"] is False
    assert fake.created["Labels"]["opr.review"] == "1"
    assert fake.removed == ["cid123"]


def test_run_review_job_fail_closed_on_docker_error(staged) -> None:
    _, job_id, ref = staged

    class Boom(FakeDocker):
        def create_container(self, name: str, config: dict[str, Any]) -> str:
            raise DockerApiError("nope", status_code=500)

    with pytest.raises(LauncherJobError, match="docker error") as excinfo:
        run_review_job(job_id=job_id, checkout_ref=ref, proxy_token="opr_job_test_token_ok", client=Boom())  # type: ignore[arg-type]
    assert excinfo.value.code == "CODEX_SANDBOX"


def test_run_review_job_rejects_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    with pytest.raises(LauncherJobError):
        run_review_job(job_id="abc", checkout_ref="../etc", proxy_token="opr_job_test_token_ok", client=FakeDocker())  # type: ignore[arg-type]


def test_run_review_job_allows_short_checkout_ttl_with_leases(staged, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-job checkout TTL couple removed; leases cover live dirs."""
    base, job_id, ref = staged
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "200")
    (base / job_id / "job.json").write_text(
        json.dumps({"model": "gpt-test", "timeout_seconds": 30}),
        encoding="utf-8",
    )
    fake = FakeDocker()

    def write_out(*args, **kwargs):
        out = base / job_id / ".opr-work" / "out.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('{"findings":[]}', encoding="utf-8")
        return 0

    fake.wait_container = lambda container_id, timeout_seconds=0: write_out()  # type: ignore[method-assign]
    result = run_review_job(
        job_id=job_id,
        checkout_ref=ref,
        proxy_token="opr_job_test_token_ok",
        client=fake,  # type: ignore[arg-type]
    )
    assert result["status"] == "ok"


def test_run_review_job_rejects_timeout_above_max(staged, monkeypatch: pytest.MonkeyPatch) -> None:
    base, job_id, ref = staged
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "120")
    monkeypatch.delenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", raising=False)
    (base / job_id / "job.json").write_text(
        json.dumps({"model": "gpt-test", "timeout_seconds": 600}),
        encoding="utf-8",
    )
    with pytest.raises(LauncherJobError, match="OPR_MAX_REVIEW_TIMEOUT_SECONDS"):
        run_review_job(
            job_id=job_id,
            checkout_ref=ref,
            proxy_token="opr_job_test_token_ok",
            client=FakeDocker(),
        )


def test_run_review_job_maps_exit_44_to_codex_timeout(staged) -> None:
    """ARB-27-022: entrypoint timeout exit 44 -> CODEX_TIMEOUT (not CODEX_SANDBOX)."""
    _, job_id, ref = staged
    fake = FakeDocker()
    fake.wait_code = 44
    fake.logs = lambda container_id, tail=200: "opr-review: timed out after 30s"  # type: ignore[method-assign]

    with pytest.raises(LauncherJobError, match="timed out") as excinfo:
        run_review_job(job_id=job_id, checkout_ref=ref, proxy_token="opr_job_test_token_ok", client=fake)  # type: ignore[arg-type]
    assert excinfo.value.code == "CODEX_TIMEOUT"
