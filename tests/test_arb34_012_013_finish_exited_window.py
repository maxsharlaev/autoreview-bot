"""ARB34-012/013: finish-scheduled revoke; exited skip orphan TTL; derived retry window."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from app.launcher.revoke_scheduler import RevokeJobStatus
from app.services.model_proxy_client import RevokeAttemptOutcome


@pytest.fixture(autouse=True)
def _reset_revoke_scheduler():
    from app.launcher import revoke_scheduler as rs

    rs._reset_for_tests()
    yield
    rs._reset_for_tests()


def _fake_docker(containers: list[dict], removed: list[str], killed: list[str] | None = None):
    killed = killed if killed is not None else []

    class FakeDocker:
        def list_review_containers(self):
            return list(containers)

        def kill_container(self, cid):
            killed.append(cid)
            for c in containers:
                if c.get("Id") == cid:
                    c["State"] = "exited"
            return None

        def remove_container(self, cid, force=False):
            removed.append(cid)
            containers[:] = [c for c in containers if c.get("Id") != cid]
            return None

        def close(self):
            return None

    return FakeDocker()


def test_exited_young_container_removed_after_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful/finished review: exited container younger than orphan TTL is deleted after confirm."""
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "1")
    # Orphan TTL is huge — exited must not wait for it (ARB34-012).
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 7200)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)

    async def done(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", done)

    job = "a" * 32
    created = time.time() - 30  # young vs 7200 TTL
    containers = [
        {
            "Id": "cid-exited-young",
            "Created": created,
            "State": "exited",
            "Names": ["/opr-review-a"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": job,
                "opr.created_unix": str(int(created)),
            },
        }
    ]
    removed: list[str] = []
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    # Allow background revoke to confirm
    deadline = time.time() + 2.0
    while time.time() < deadline and rs.revoke_status(job, created_at=created) is not RevokeJobStatus.CONFIRMED:
        time.sleep(0.05)
    assert rs.revoke_status(job, created_at=created) is RevokeJobStatus.CONFIRMED
    # Second pass deletes after confirm (first may have deferred if confirm raced)
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    if "cid-exited-young" not in removed:
        # confirm may have landed between passes — one more
        time.sleep(0.1)
        reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    assert "cid-exited-young" in removed


def test_exited_proxy_down_retained_until_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proxy down: exited container retained until retry window, then deleted."""
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 7200)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)

    async def transient(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.TRANSIENT

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", transient)

    job = "b" * 32
    created = time.time() - 10
    containers = [
        {
            "Id": "cid-proxy-down",
            "Created": created,
            "State": "exited",
            "Names": ["/opr-review-b"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": job,
                "opr.created_unix": str(int(created)),
            },
        }
    ]
    removed: list[str] = []
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    time.sleep(0.1)
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    assert "cid-proxy-down" not in removed
    assert rs.revoke_status(job, created_at=created) in {
        RevokeJobStatus.PENDING,
        RevokeJobStatus.CONFIG_ERROR,
    }

    # Expire token → delete (ARB34-014)
    old_created = time.time() - 100
    old_expires = time.time() - 90
    containers[0]["Created"] = old_created
    containers[0]["Labels"]["opr.created_unix"] = str(int(old_created))
    containers[0]["Labels"]["opr.token_expires_unix"] = str(int(old_expires))
    rs._reset_for_tests()
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    assert "cid-proxy-down" in removed
    assert rs.revoke_status(job, created_at=old_created, expires_at=old_expires) is RevokeJobStatus.WINDOW_EXPIRED


def test_running_young_orphan_waits_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running orphan without finish still waits for orphan TTL."""
    from app.launcher import reaper as reaper_mod

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "3600")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 7200)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)

    async def done(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", done)

    job = "c" * 32
    created = time.time() - 30
    containers = [
        {
            "Id": "cid-running-young",
            "Created": created,
            "State": "running",
            "Names": ["/opr-review-c"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": job,
                "opr.created_unix": str(int(created)),
            },
        }
    ]
    removed: list[str] = []
    killed: list[str] = []
    reaper_mod.reap_once(client=_fake_docker(containers, removed, killed))  # type: ignore[arg-type]
    assert killed == []
    assert removed == []


def test_exited_after_scheduler_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulated launcher restart: in-memory revoke state cleared; exited still reaped after confirm."""
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 7200)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)

    async def done(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", done)

    job = "d" * 32
    created = time.time() - 5
    containers = [
        {
            "Id": "cid-restart",
            "Created": created,
            "State": "exited",
            "Names": ["/opr-review-d"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": job,
                "opr.created_unix": str(int(created)),
            },
        }
    ]
    removed: list[str] = []
    # First process "crash": clear scheduler memory (restart)
    rs._reset_for_tests()
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    deadline = time.time() + 2.0
    while time.time() < deadline and rs.revoke_status(job, created_at=created) is not RevokeJobStatus.CONFIRMED:
        time.sleep(0.05)
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    if "cid-restart" not in removed:
        time.sleep(0.1)
        reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    assert "cid-restart" in removed


def test_run_review_job_schedules_revoke_on_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finish path schedules revoke so reaper can confirm-and-delete without waiting TTL."""
    from app.launcher import jobs as jobs_mod
    from app.launcher import revoke_scheduler as rs
    from app.launcher.job_lifecycle import JobLifecycleRegistry

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "900")
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "1")
    jobs_mod.lifecycle = JobLifecycleRegistry()
    job_id = "e" * 32
    job_dir = tmp_path / job_id
    repo = job_dir / "repo"
    repo.mkdir(parents=True)
    (job_dir / ".opr-work").mkdir(parents=True, exist_ok=True)
    (job_dir / "prompt.txt").write_text("p", encoding="utf-8")
    (job_dir / "schema.json").write_text("{}", encoding="utf-8")
    created = int(time.time()) - 2
    (job_dir / "job.json").write_text(
        f'{{"model":"m","timeout_seconds":30,"created_unix":{created}}}',
        encoding="utf-8",
    )

    scheduled: list[tuple[str, float | None]] = []
    real_schedule = rs.schedule_proxy_token_revoke

    def track_schedule(jid: str, *, created_at: float | None = None, **kwargs: Any) -> bool:
        scheduled.append((jid, created_at))
        return real_schedule(jid, created_at=created_at, **kwargs)

    monkeypatch.setattr(rs, "schedule_proxy_token_revoke", track_schedule)
    # Also patch the import site used inside finally
    monkeypatch.setattr("app.launcher.revoke_scheduler.schedule_proxy_token_revoke", track_schedule)

    async def done(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", done)

    class FakeDocker:
        def create_container(self, name, config):
            return "cid-finish"

        def start_container(self, cid):
            return None

        def wait_container(self, cid, timeout_seconds=None):
            work_out = tmp_path / job_id / ".opr-work"
            work_out.mkdir(parents=True, exist_ok=True)
            (work_out / "out.json").write_text('{"ok":1}', encoding="utf-8")
            return 0

        def logs(self, cid):
            return ""

        def kill_container(self, cid, timeout=None):
            return None

        def remove_container(self, cid, force=True, timeout=None):
            raise AssertionError("finish must not remove_container")

        def close(self):
            return None

    monkeypatch.setenv("OPR_CHECKOUT_VOLUME", "opr_checkouts")
    monkeypatch.setattr(jobs_mod, "assert_safe_host_config", lambda hc: None)
    monkeypatch.setattr(
        jobs_mod,
        "build_host_config",
        lambda **kw: {"Binds": [], "NetworkMode": "bridge"},
    )
    monkeypatch.setattr(
        "app.launcher.codex_config.render_opr_proxy_config",
        lambda: "#test",
    )

    result = jobs_mod.run_review_job(
        job_id=job_id,
        checkout_ref=f"{job_id}/repo",
        proxy_token="opr_job_" + "t" * 40,
        client=FakeDocker(),  # type: ignore[arg-type]
    )
    assert result["status"] == "ok"
    assert any(jid == job_id for jid, _ in scheduled)
    # created_at should be near job.json
    cats = [c for jid, c in scheduled if jid == job_id]
    assert cats and cats[0] is not None
    assert abs(float(cats[0]) - float(created)) < 2


def test_derived_window_timeout_1200_is_1260(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", raising=False)
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "1200")
    monkeypatch.setenv("PROXY_TOKEN_GRACE_SECONDS", "60")
    from app.launcher.revoke_scheduler import (
        derived_revoke_retry_window_seconds,
        revoke_retry_window_seconds,
    )

    assert derived_revoke_retry_window_seconds() == 1260.0
    assert revoke_retry_window_seconds() == 1260.0


def test_explicit_window_smaller_than_derived_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "1200")
    monkeypatch.setenv("PROXY_TOKEN_GRACE_SECONDS", "60")
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "600")
    from app.launcher.revoke_scheduler import ensure_revoke_retry_window_config

    with pytest.raises(RuntimeError, match="LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS"):
        ensure_revoke_retry_window_config()


def test_explicit_window_at_least_derived_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "1200")
    monkeypatch.setenv("PROXY_TOKEN_GRACE_SECONDS", "60")
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "1260")
    from app.launcher.revoke_scheduler import (
        ensure_revoke_retry_window_config,
        revoke_retry_window_seconds,
    )

    ensure_revoke_retry_window_config()
    assert revoke_retry_window_seconds() == 1260.0
