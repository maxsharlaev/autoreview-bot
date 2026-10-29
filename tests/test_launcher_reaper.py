"""Launcher reaper: containers + staged checkout UUID dirs."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.launcher.reaper import (
    is_uuid_job_dirname,
    reap_checkout_orphans,
    reap_once,
)


def test_is_uuid_job_dirname() -> None:
    assert is_uuid_job_dirname(uuid.uuid4().hex)
    assert is_uuid_job_dirname(str(uuid.uuid4()))
    assert not is_uuid_job_dirname("ai-review-abc")
    assert not is_uuid_job_dirname("short")
    assert not is_uuid_job_dirname("../etc")


def test_reap_checkout_removes_old_orphan(tmp_path: Path) -> None:
    job = uuid.uuid4().hex
    d = tmp_path / job
    d.mkdir()
    (d / "repo").mkdir()
    old = time.time() - 10_000
    os.utime(d, (old, old))
    removed = reap_checkout_orphans(base_dir=tmp_path, ttl_seconds=60, live_job_ids=set())
    assert removed == 1
    assert not d.exists()


def test_reap_checkout_keeps_active_job(tmp_path: Path) -> None:
    job = uuid.uuid4().hex
    d = tmp_path / job
    d.mkdir()
    old = time.time() - 10_000
    os.utime(d, (old, old))
    removed = reap_checkout_orphans(base_dir=tmp_path, ttl_seconds=60, live_job_ids={job})
    assert removed == 0
    assert d.exists()


def test_reap_checkout_keeps_young_dir(tmp_path: Path) -> None:
    job = uuid.uuid4().hex
    d = tmp_path / job
    d.mkdir()
    removed = reap_checkout_orphans(base_dir=tmp_path, ttl_seconds=3600, live_job_ids=set())
    assert removed == 0
    assert d.exists()


def test_reap_checkout_ignores_non_uuid_and_outside_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside_secret"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET_CANARY\n", encoding="utf-8")
    non_uuid = tmp_path / "ai-review-keepme"
    non_uuid.mkdir()
    (non_uuid / "f").write_text("ok\n", encoding="utf-8")
    # Symlink with non-UUID name pointing outside — must not be followed or removed.
    link = tmp_path / "not-a-uuid-link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not available on this host")
    removed = reap_checkout_orphans(base_dir=tmp_path, ttl_seconds=1, live_job_ids=set())
    assert removed == 0
    assert non_uuid.exists()
    assert link.exists()
    assert outside.exists()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "SECRET_CANARY\n"


def test_reap_checkout_unlinks_uuid_symlink_without_following(tmp_path: Path) -> None:
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET_CANARY\n", encoding="utf-8")
    job = uuid.uuid4().hex
    link = tmp_path / job
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not available on this host")
    old = time.time() - 10_000
    # utime on symlink may touch target on some OS; age via lstat used by reaper.
    try:
        os.utime(link, (old, old), follow_symlinks=False)
    except (TypeError, NotImplementedError, OSError):
        # If we cannot age the symlink, force ttl=0 path by monkeypatching time — skip.
        pytest.skip("cannot set symlink mtime without following")
    removed = reap_checkout_orphans(base_dir=tmp_path, ttl_seconds=60, live_job_ids=set())
    assert removed == 1
    assert not link.exists()
    assert outside.exists()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "SECRET_CANARY\n"


def test_reap_once_runs_checkout_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "60")
    job = uuid.uuid4().hex
    d = tmp_path / job
    d.mkdir()
    old = time.time() - 10_000
    os.utime(d, (old, old))

    class FakeDocker:
        def list_review_containers(self) -> list[dict[str, Any]]:
            return []

        def close(self) -> None:
            return None

    removed = reap_once(client=FakeDocker())  # type: ignore[arg-type]
    assert removed >= 1
    assert not d.exists()
