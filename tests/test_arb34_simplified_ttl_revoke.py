"""Simplified M3 revoke model: TTL = timeout + grace (default 30s); best-effort only."""

from __future__ import annotations

import time
from typing import Any

import pytest


def test_proxy_token_grace_default_is_30(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.model_proxy import settings as st

    monkeypatch.delenv("PROXY_TOKEN_GRACE_SECONDS", raising=False)
    monkeypatch.delenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", raising=False)
    cfg = st.load_settings()
    assert cfg.token_grace_seconds == 30
    assert cfg.max_token_ttl_seconds == cfg.max_review_timeout_seconds + 30


def test_token_ttl_is_timeout_plus_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.model_proxy import settings as st

    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("PROXY_TOKEN_GRACE_SECONDS", "30")
    cfg = st.load_settings()
    assert cfg.max_token_ttl_seconds == 150


def test_cancel_deadline_holds_with_hanging_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.launcher import cancel_scheduler as cs
    from app.launcher.job_lifecycle import lifecycle

    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "0.3")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "5")
    cs._reset_for_tests()
    # Unique id so a leftover pool cancel cannot coalesce into other suite tests.
    job = "aa11" + ("b" * 28)

    def slow_cancel(**_k: Any) -> dict:
        time.sleep(2.0)
        return {"job_id": job, "removed": 0, "confirmed": True}

    monkeypatch.setattr("app.launcher.jobs.cancel_review_job", slow_cancel)
    monkeypatch.setattr(lifecycle, "request_cancel", lambda jid: None)

    async def hang(*, job_id: str, **_kwargs: Any):
        import asyncio

        await asyncio.sleep(30)

    monkeypatch.setattr(
        "app.services.model_proxy_client.revoke_job_token_attempt",
        hang,
    )
    # Also hang the plain revoke path if used
    monkeypatch.setattr(
        "app.services.model_proxy_client.revoke_job_token",
        hang,
    )

    started = time.monotonic()
    result = cs.cancel_review_job_bounded(job_id=job)
    elapsed = time.monotonic() - started
    assert result["confirmed"] is False
    assert elapsed < 1.5
    cs._reset_for_tests()


def test_reaper_never_blocks_on_hanging_revoke(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.launcher import reaper as reaper_mod

    scheduled: list[str] = []

    def fake_schedule(job_id: str) -> None:
        scheduled.append(job_id)
        time.sleep(0)  # must return immediately

    monkeypatch.setattr(reaper_mod, "schedule_proxy_token_revoke", fake_schedule)
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)

    removed: list[str] = []

    class FakeDocker:
        def list_review_containers(self):
            return [
                {
                    "Id": "aaaa",
                    "Created": 0,
                    "Names": ["/opr-review-a"],
                    "Labels": {"opr.review": "1", "opr.job_id": "b" * 32},
                },
                {
                    "Id": "bbbb",
                    "Created": 0,
                    "Names": ["/opr-review-b"],
                    "Labels": {"opr.review": "1", "opr.job_id": "c" * 32},
                },
            ]

        def kill_container(self, cid):
            return None

        def remove_container(self, cid, force=False):
            removed.append(cid)

        def close(self):
            return None

    started = time.monotonic()
    reaper_mod.reap_once(client=FakeDocker())  # type: ignore[arg-type]
    assert time.monotonic() - started < 1.0
    assert "aaaa" in removed and "bbbb" in removed
    assert len(scheduled) == 2
