"""ARB34-014: revoke window anchored on absolute token expires_at (+ skew)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from app.launcher.revoke_scheduler import RevokeJobStatus
from app.services.model_proxy_client import IssuedJobToken, RevokeAttemptOutcome


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


def test_delayed_issuance_does_not_shorten_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Early created_unix must not expire the window before expires_at + skew."""
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_REVOKE_CLOCK_SKEW_SECONDS", "30")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 7200)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)

    async def hang(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.TRANSIENT

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", hang)

    job = "a" * 32
    created = time.time() - 10_000  # far in the past (issuance latency / early staging)
    expires = time.time() + 3_600  # token still live
    containers = [
        {
            "Id": "cid-delay",
            "Created": created,
            "State": "exited",
            "Names": ["/opr-review-a"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": job,
                "opr.created_unix": str(int(created)),
                "opr.token_expires_unix": str(int(expires)),
            },
        }
    ]
    removed: list[str] = []
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    time.sleep(0.05)
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    assert "cid-delay" not in removed
    assert rs.may_delete_after_revoke(job, expires_at=expires, created_at=created) is False
    # Fresh job: past expires_at + skew → window expired (latest-wins would keep future)
    rs._reset_for_tests()
    past = time.time() - 100
    assert rs.may_delete_after_revoke("z" * 32, expires_at=past, created_at=created, now=time.time()) is True


def test_expires_at_from_label_survives_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_REVOKE_CLOCK_SKEW_SECONDS", "5")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 7200)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)

    async def done(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.TRANSIENT

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", done)

    job = "b" * 32
    created = time.time() - 50
    expires = time.time() - 60  # already expired (+skew)
    containers = [
        {
            "Id": "cid-restart-exp",
            "Created": created,
            "State": "exited",
            "Names": ["/opr-review-b"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": job,
                "opr.created_unix": str(int(created)),
                "opr.token_expires_unix": str(int(expires)),
            },
        }
    ]
    removed: list[str] = []
    # Simulate launcher restart: empty in-memory scheduler
    rs._reset_for_tests()
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    assert "cid-restart-exp" in removed
    assert rs.revoke_status(job, expires_at=expires, created_at=created) is RevokeJobStatus.WINDOW_EXPIRED


def test_job_json_token_expires_unix_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_REVOKE_CLOCK_SKEW_SECONDS", "5")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)
    monkeypatch.setattr(reaper_mod, "directory_is_lease_stale", lambda path, now=None: True)

    async def done(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.TRANSIENT

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", done)

    job = "c" * 32
    keep = tmp_path / job
    keep.mkdir()
    created = time.time() - 100
    expires = time.time() - 90
    (keep / "job.json").write_text(
        f'{{"created_unix": {int(created)}, "token_expires_unix": {expires}}}',
        encoding="utf-8",
    )
    import os

    os.utime(keep, (created, created))
    rs._reset_for_tests()
    n = reaper_mod.reap_checkout_orphans(live_job_ids=set(), now=time.time())
    assert n >= 1
    assert not keep.exists()


def test_old_job_json_without_expires_uses_orphan_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old format: no token_expires_unix → retain until orphan TTL gate (not created+window)."""
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "1")  # would fire if created used
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 7200)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)

    async def hang(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.TRANSIENT

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", hang)

    job = "d" * 32
    created = time.time() - 100  # older than window=1 but younger than orphan TTL
    containers = [
        {
            "Id": "cid-old-fmt",
            "Created": created,
            "State": "exited",
            "Names": ["/opr-review-d"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": job,
                "opr.created_unix": str(int(created)),
                # no opr.token_expires_unix
            },
        }
    ]
    removed: list[str] = []
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    time.sleep(0.05)
    reaper_mod.reap_once(client=_fake_docker(containers, removed))  # type: ignore[arg-type]
    assert "cid-old-fmt" not in removed
    # Orphan TTL elapsed → may delete
    assert rs.may_delete_after_revoke(job, created_at=created, orphan_ttl_elapsed=True) is True


@pytest.mark.asyncio
async def test_issued_job_token_prefers_proxy_expires_at(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    import respx
    from app.services import model_proxy_client as mpc

    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16chars")
    expires = 1_700_000_000.0

    with respx.mock:
        respx.post("http://model-proxy:8081/internal/jobs/job1/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "job_id": "job1",
                    "token": "opr_job_" + "x" * 40,
                    "expires_in": 100,
                    "expires_at": expires,
                    "owner_id": "",
                },
            )
        )
        issued = await mpc.issue_job_token(job_id="job1", timeout_seconds=60)
    assert isinstance(issued, IssuedJobToken)
    assert issued.expires_at == expires


def test_latest_expires_at_wins() -> None:
    from app.launcher import revoke_scheduler as rs

    rs._reset_for_tests()
    job = "e" * 32
    rs.schedule_proxy_token_revoke(job, expires_at=1000.0)
    rs.schedule_proxy_token_revoke(job, expires_at=2000.0)  # later = more conservative
    assert rs.effective_token_expires_at(job) == 2000.0
    # Earlier must not shrink
    rs.schedule_proxy_token_revoke(job, expires_at=1500.0)
    assert rs.effective_token_expires_at(job) == 2000.0
