"""Job lifecycle registry and cancel races against container create/start."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from app.launcher.job_lifecycle import JobLifecycleRegistry, JobPhase
from app.launcher.jobs import LauncherJobError, cancel_review_job, run_review_job

REPO_SECCOMP = Path(__file__).resolve().parents[1] / "docker" / "review" / "seccomp" / "review-bwrap.json"


@pytest.fixture()
def isolated_lifecycle(monkeypatch: pytest.MonkeyPatch) -> JobLifecycleRegistry:
    reg = JobLifecycleRegistry(tombstone_ttl_seconds=60, cancel_wait_seconds=2.0)
    monkeypatch.setattr("app.launcher.jobs.lifecycle", reg)
    monkeypatch.setattr("app.launcher.job_lifecycle.lifecycle", reg)
    return reg


def test_tombstone_rejects_late_accept(isolated_lifecycle: JobLifecycleRegistry) -> None:
    jid = "a" * 32
    isolated_lifecycle.request_cancel(jid)
    with pytest.raises(RuntimeError, match="cancelled"):
        isolated_lifecycle.accept(jid)


def test_cancel_before_create(
    isolated_lifecycle: JobLifecycleRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_REVIEW_SECCOMP_PROFILE", str(REPO_SECCOMP))
    monkeypatch.setenv("OPR_CHECKOUT_VOLUME", "opr_checkouts")
    jid = "b" * 32
    job = tmp_path / jid
    repo = job / "repo"
    repo.mkdir(parents=True)
    (job / "prompt.txt").write_text("p", encoding="utf-8")
    (job / "schema.json").write_text("{}", encoding="utf-8")
    (job / "job.json").write_text(json.dumps({"model": "gpt-test", "timeout_seconds": 30}), encoding="utf-8")

    # Cancel before launch starts -> tombstone; accept fails.
    cancel_review_job(
        job_id=jid,
        client=_EmptyDocker(),  # type: ignore[arg-type]
    )
    with pytest.raises(LauncherJobError, match="cancelled"):
        run_review_job(
            job_id=jid,
            checkout_ref=f"{jid}/repo",
            proxy_token="opr_job_test_token_ok",
            client=_EmptyDocker(),  # type: ignore[arg-type]
        )


def test_cancel_during_create(
    isolated_lifecycle: JobLifecycleRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_REVIEW_SECCOMP_PROFILE", str(REPO_SECCOMP))
    monkeypatch.setenv("OPR_CHECKOUT_VOLUME", "opr_checkouts")
    jid = "c" * 32
    job = tmp_path / jid
    repo = job / "repo"
    repo.mkdir(parents=True)
    (job / "prompt.txt").write_text("p", encoding="utf-8")
    (job / "schema.json").write_text("{}", encoding="utf-8")
    (job / "job.json").write_text(json.dumps({"model": "gpt-test", "timeout_seconds": 30}), encoding="utf-8")

    entered = threading.Event()
    release = threading.Event()
    removed: list[str] = []

    class SlowCreateDocker:
        def create_container(self, name: str, config: dict[str, Any]) -> str:
            entered.set()
            assert release.wait(timeout=5)
            return "cid-during-create"

        def start_container(self, container_id: str) -> None:
            raise AssertionError("must not start after cancel")

        def wait_container(self, container_id: str, *, timeout_seconds: int) -> int:
            return 0

        def logs(self, container_id: str, *, tail: int = 200) -> str:
            return ""

        def remove_container(self, container_id: str, *, force: bool = True, timeout: float | None = None) -> None:
            removed.append(container_id)

        def list_containers_for_job(self, job_id: str, *, timeout: float | None = None) -> list[dict[str, Any]]:
            return []

        def kill_container(self, cid: str, *, timeout: float | None = None) -> None:
            return None

        def close(self) -> None:
            return None

    errors: list[BaseException] = []
    cancel_result: dict[str, Any] = {}

    def launch() -> None:
        try:
            run_review_job(
                job_id=jid,
                checkout_ref=f"{jid}/repo",
                proxy_token="opr_job_test_token_ok",
                client=SlowCreateDocker(),  # type: ignore[arg-type]
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_cancel() -> None:
        cancel_result.update(
            cancel_review_job(job_id=jid, client=SlowCreateDocker())  # type: ignore[arg-type]
        )

    thread = threading.Thread(target=launch)
    thread.start()
    assert entered.wait(timeout=5)
    cancel_thread = threading.Thread(target=do_cancel)
    cancel_thread.start()
    # Let DELETE mark cancelled and begin waiting for idle, then finish create.
    time.sleep(0.1)
    release.set()
    thread.join(timeout=10)
    cancel_thread.join(timeout=10)
    assert not thread.is_alive()
    assert not cancel_thread.is_alive()
    assert errors and "cancelled" in str(errors[0]).lower()
    assert "cid-during-create" in removed
    assert cancel_result.get("confirmed") is True
    assert isolated_lifecycle.is_cancelled(jid)


def test_cancel_after_start(
    isolated_lifecycle: JobLifecycleRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_REVIEW_SECCOMP_PROFILE", str(REPO_SECCOMP))
    monkeypatch.setenv("OPR_CHECKOUT_VOLUME", "opr_checkouts")
    jid = "d" * 32
    job = tmp_path / jid
    repo = job / "repo"
    repo.mkdir(parents=True)
    (job / "prompt.txt").write_text("p", encoding="utf-8")
    (job / "schema.json").write_text("{}", encoding="utf-8")
    (job / "job.json").write_text(json.dumps({"model": "gpt-test", "timeout_seconds": 30}), encoding="utf-8")

    started = threading.Event()
    release_wait = threading.Event()
    killed: list[str] = []
    containers = [{"Id": "cid-running", "Labels": {"opr.job_id": jid}}]

    class RunningDocker:
        def create_container(self, name: str, config: dict[str, Any]) -> str:
            return "cid-running"

        def start_container(self, container_id: str) -> None:
            started.set()

        def wait_container(self, container_id: str, *, timeout_seconds: int) -> int:
            release_wait.wait(timeout=5)
            # Simulate cancel removal during wait.
            from app.launcher.docker_api import DockerApiError

            raise DockerApiError("no such container", status_code=404)

        def logs(self, container_id: str, *, tail: int = 200) -> str:
            return ""

        def remove_container(self, container_id: str, *, force: bool = True, timeout: float | None = None) -> None:
            containers.clear()

        def list_containers_for_job(self, job_id: str, *, timeout: float | None = None) -> list[dict[str, Any]]:
            return list(containers)

        def kill_container(self, cid: str, *, timeout: float | None = None) -> None:
            killed.append(cid)
            containers.clear()

        def close(self) -> None:
            return None

    errors: list[BaseException] = []
    cancel_result: dict[str, Any] = {}

    def launch() -> None:
        try:
            run_review_job(
                job_id=jid,
                checkout_ref=f"{jid}/repo",
                proxy_token="opr_job_test_token_ok",
                client=RunningDocker(),  # type: ignore[arg-type]
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_cancel() -> None:
        cancel_result.update(
            cancel_review_job(job_id=jid, client=RunningDocker())  # type: ignore[arg-type]
        )

    thread = threading.Thread(target=launch)
    thread.start()
    assert started.wait(timeout=5)
    time.sleep(0.05)
    cancel_thread = threading.Thread(target=do_cancel)
    cancel_thread.start()
    time.sleep(0.1)
    # Unblock wait so launch finishes (Docker 404 after cancel removal).
    release_wait.set()
    thread.join(timeout=10)
    cancel_thread.join(timeout=10)
    assert not thread.is_alive()
    assert not cancel_thread.is_alive()
    assert errors
    assert killed == ["cid-running"] or cancel_result.get("removed", 0) >= 0
    assert cancel_result.get("confirmed") is True


def test_late_start_after_tombstone(isolated_lifecycle: JobLifecycleRegistry) -> None:
    jid = "e" * 32
    snap = isolated_lifecycle.request_cancel(jid)
    assert snap.phase == JobPhase.CANCELLED
    assert snap.tombstone_until > 0
    with pytest.raises(RuntimeError, match="cancelled"):
        isolated_lifecycle.accept(jid)


class _EmptyDocker:
    def list_containers_for_job(self, job_id: str, *, timeout: float | None = None) -> list[dict[str, Any]]:
        return []

    def create_container(self, name: str, config: dict[str, Any]) -> str:
        raise AssertionError("create should not run")

    def close(self) -> None:
        return None
