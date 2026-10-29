"""Checkout/job directory lease heartbeats and reaper policy."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
from app.launcher.lease import (
    LEASE_FILENAME,
    directory_is_lease_stale,
    lease_stale_after_seconds,
    no_lease_max_age_seconds,
    start_directory_lease,
    stop_directory_lease,
    touch_lease,
)
from app.launcher.reaper import reap_checkout_orphans


@pytest.mark.asyncio
async def test_fresh_lease_keeps_dir_during_second_reap_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPR_LEASE_HEARTBEAT_SECONDS", "5")
    name = "ai-review-" + uuid.uuid4().hex
    d = tmp_path / name
    d.mkdir()
    await start_directory_lease(d, heartbeat_seconds=5)
    try:
        # Age the directory itself; lease is fresh.
        old = time.time() - 10_000
        os.utime(d, (old, old))
        assert reap_checkout_orphans(base_dir=tmp_path, live_job_ids=set()) == 0
        assert d.exists()
        # Second attempt still keeps while lease is fresh.
        assert reap_checkout_orphans(base_dir=tmp_path, live_job_ids=set()) == 0
        assert d.exists()
    finally:
        await stop_directory_lease(d)


def test_stale_lease_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_LEASE_HEARTBEAT_SECONDS", "5")
    name = "ai-review-" + uuid.uuid4().hex
    d = tmp_path / name
    d.mkdir()
    touch_lease(d)
    lease = d / LEASE_FILENAME
    stale = time.time() - lease_stale_after_seconds() - 10
    os.utime(lease, (stale, stale))
    assert directory_is_lease_stale(d) is True
    assert reap_checkout_orphans(base_dir=tmp_path, live_job_ids=set()) == 1
    assert not d.exists()


def test_no_lease_young_dir_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", raising=False)
    name = uuid.uuid4().hex
    d = tmp_path / name
    d.mkdir()
    assert (d / LEASE_FILENAME).exists() is False
    assert directory_is_lease_stale(d) is False
    assert reap_checkout_orphans(base_dir=tmp_path, live_job_ids=set()) == 0
    assert d.exists()


def test_no_lease_old_dir_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "120")
    name = uuid.uuid4().hex
    d = tmp_path / name
    d.mkdir()
    old = time.time() - no_lease_max_age_seconds() - 10
    os.utime(d, (old, old))
    assert reap_checkout_orphans(base_dir=tmp_path, live_job_ids=set()) == 1
    assert not d.exists()
