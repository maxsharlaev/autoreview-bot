"""ARB34-008: reaper must not stall on a hanging proxy revoke."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


def test_reaper_continues_when_one_revoke_hangs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One hanging revoke must not block removing other containers/checkouts."""
    from app.launcher import reaper as reaper_mod

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "1")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)
    monkeypatch.setattr(reaper_mod, "directory_is_lease_stale", lambda path, now=None: True)

    scheduled: list[str] = []
    monkeypatch.setattr(
        reaper_mod,
        "schedule_proxy_token_revoke",
        lambda jid, created_at=None: (scheduled.append(jid), True)[1],
    )
    monkeypatch.setattr(
        reaper_mod,
        "may_delete_after_revoke",
        lambda jid, created_at=None, now=None: True,
    )
    monkeypatch.setattr(reaper_mod, "drain_revoke_retries", lambda: 0)

    sync_called = {"n": 0}

    def boom_sync(**_kwargs):
        sync_called["n"] += 1
        time.sleep(30)
        return False

    import app.services.model_proxy_client as mpc

    monkeypatch.setattr(mpc, "revoke_job_token_sync", boom_sync)

    hang_job = "a" * 32
    other_job = "b" * 32
    other_dir = tmp_path / other_job
    other_dir.mkdir()
    old = time.time() - 3600
    os.utime(other_dir, (old, old))

    removed_cids: list[str] = []

    class FakeDocker:
        def list_review_containers(self):
            return [
                {
                    "Id": "aaaa",
                    "Created": 0,
                    "Names": ["/opr-review-a"],
                    "Labels": {"opr.review": "1", "opr.job_id": hang_job},
                },
                {
                    "Id": "bbbb",
                    "Created": 0,
                    "Names": ["/opr-review-b"],
                    "Labels": {"opr.review": "1", "opr.job_id": other_job},
                },
            ]

        def kill_container(self, cid):
            return None

        def remove_container(self, cid, force=False):
            removed_cids.append(cid)
            return None

        def close(self):
            return None

    t0 = time.monotonic()
    removed = reaper_mod.reap_once(client=FakeDocker())  # type: ignore[arg-type]
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, f"reaper stalled for {elapsed:.2f}s (likely sync revoke)"
    assert removed >= 2
    assert "aaaa" in removed_cids and "bbbb" in removed_cids
    assert hang_job in scheduled and other_job in scheduled
    assert not other_dir.exists()
    assert sync_called["n"] == 0
