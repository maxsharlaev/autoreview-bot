"""Directory lease heartbeats for checkout/job dirs (worker + launcher reaper)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Sidecar next to the directory (not inside the worktree): ``<dirname>.lease``.
# Clone checkouts are git worktrees; a tracked top-level ``.lease`` must not collide.
LEASE_SIDECAR_SUFFIX = ".lease"
# Back-compat alias used by older tests/docs referring to the lease basename suffix.
LEASE_FILENAME = LEASE_SIDECAR_SUFFIX
DEFAULT_LEASE_HEARTBEAT_SECONDS = 30
LEASE_STALE_MULTIPLIER = 3
# When a dir has no lease (crash before first heartbeat), use this age floor.
DEFAULT_NO_LEASE_MAX_AGE_SECONDS = 900


def lease_heartbeat_seconds() -> int:
    raw = os.environ.get("OPR_LEASE_HEARTBEAT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_LEASE_HEARTBEAT_SECONDS
    try:
        return max(5, int(raw))
    except ValueError:
        return DEFAULT_LEASE_HEARTBEAT_SECONDS


def lease_stale_after_seconds() -> int:
    """Reap when now - lease mtime exceeds this (heartbeat * N)."""
    return lease_heartbeat_seconds() * LEASE_STALE_MULTIPLIER


def no_lease_max_age_seconds() -> int:
    """Conservative age bound for dirs that never got a lease sidecar.

    Uses OPR_CHECKOUT_ORPHAN_TTL_SECONDS when set; otherwise a fixed default
    (not coupled to clone+review+overhead — leases cover live work).
    """
    raw = os.environ.get("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "").strip()
    if raw:
        try:
            return max(60, int(raw))
        except ValueError:
            pass
    return max(DEFAULT_NO_LEASE_MAX_AGE_SECONDS, lease_stale_after_seconds() * 2)


def lease_path(directory: Path) -> Path:
    """Sidecar lease file beside ``directory`` (``<parent>/<name>.lease``)."""
    d = Path(directory)
    return d.parent / f"{d.name}{LEASE_SIDECAR_SUFFIX}"


def touch_lease(directory: Path) -> None:
    """Create/update the sidecar lease file (no symlink follow)."""
    path = lease_path(directory)
    try:
        if path.is_symlink() or path.parent.is_symlink():
            logger.warning("lease path is a symlink; refusing directory=%s", directory)
            return
        path.write_bytes(b"")
        os.utime(path, None)
    except OSError as exc:
        logger.warning("lease touch failed directory=%s: %s", directory, exc)


def remove_directory_lease_file(directory: Path) -> None:
    """Delete the sidecar lease file if present (idempotent)."""
    path = lease_path(directory)
    try:
        if path.is_symlink():
            return
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("lease unlink failed directory=%s: %s", directory, exc)


def lease_mtime(directory: Path) -> float | None:
    """Return sidecar lease mtime via lstat, or None if missing/unreadable."""
    path = lease_path(directory)
    try:
        st = path.lstat()
    except OSError:
        return None
    if path.is_symlink():
        return None
    return float(st.st_mtime)


def directory_is_lease_stale(directory: Path, *, now: float | None = None) -> bool:
    """True when the directory should be reaped under the lease policy."""
    clock = time.time() if now is None else float(now)
    mtime = lease_mtime(directory)
    if mtime is not None:
        return (clock - mtime) > float(lease_stale_after_seconds())
    try:
        st = Path(directory).lstat()
    except OSError:
        return False
    return (clock - float(st.st_mtime)) > float(no_lease_max_age_seconds())


class LeaseKeeper:
    """Background heartbeat that refreshes the sidecar lease until stopped."""

    def __init__(self, directory: Path, *, heartbeat_seconds: float | None = None) -> None:
        self.directory = Path(directory)
        self.heartbeat_seconds = (
            float(heartbeat_seconds) if heartbeat_seconds is not None else float(lease_heartbeat_seconds())
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        touch_lease(self.directory)
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name=f"lease:{self.directory.name}")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=max(1.0, self.heartbeat_seconds))
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_seconds)
                break
            except TimeoutError:
                touch_lease(self.directory)


_REGISTRY: dict[str, LeaseKeeper] = {}
_REGISTRY_LOCK = asyncio.Lock()


def _key(directory: Path) -> str:
    try:
        return str(Path(directory).resolve())
    except OSError:
        return str(Path(directory))


async def start_directory_lease(directory: Path, *, heartbeat_seconds: float | None = None) -> None:
    """Start (or replace) a lease heartbeat for directory. Call right after mkdir."""
    key = _key(directory)
    async with _REGISTRY_LOCK:
        existing = _REGISTRY.pop(key, None)
        if existing is not None:
            await existing.stop()
        keeper = LeaseKeeper(directory, heartbeat_seconds=heartbeat_seconds)
        keeper.start()
        _REGISTRY[key] = keeper


async def stop_directory_lease(directory: Path) -> None:
    """Stop the lease heartbeat for directory (idempotent)."""
    key = _key(directory)
    async with _REGISTRY_LOCK:
        keeper = _REGISTRY.pop(key, None)
    if keeper is not None:
        await keeper.stop()
