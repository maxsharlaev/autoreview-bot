"""Remove orphaned review containers and staged checkout job dirs (lease-aware)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import stat
import time
from pathlib import Path

from app.launcher.docker_api import DockerApiError, DockerClient
from app.launcher.job_lifecycle import lifecycle
from app.launcher.lease import directory_is_lease_stale
from app.launcher.spec import REVIEW_LABEL, REVIEW_LABEL_VALUE
from app.launcher.timeouts import review_orphan_ttl_seconds

logger = logging.getLogger(__name__)

# uuid.uuid4().hex (32) or canonical dashed UUID (staged job dirs).
_UUID_DIR_RE = re.compile(
    r"^(?:"
    r"[0-9a-f]{32}"
    r"|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r")$",
    re.IGNORECASE,
)
# Worker clone_head dirs: ai-review-<uuid4 hex> (strict; not mkdtemp random suffix).
_CLONE_DIR_RE = re.compile(r"^ai-review-[0-9a-f]{32}$", re.IGNORECASE)


def orphan_ttl_seconds() -> int:
    """Container orphan TTL (``OPR_REVIEW_ORPHAN_TTL_SECONDS``)."""
    return review_orphan_ttl_seconds()


def reaper_interval_seconds() -> int:
    raw = os.environ.get("OPR_REVIEW_REAPER_INTERVAL_SECONDS", "300").strip()
    try:
        return max(30, int(raw))
    except ValueError:
        return 300


def checkout_base_path() -> Path:
    raw = os.environ.get("OPR_CHECKOUT_BASE", "/var/opr/checkouts").strip()
    return Path(raw)


def is_uuid_job_dirname(name: str) -> bool:
    return bool(_UUID_DIR_RE.fullmatch(name or ""))


def is_clone_dirname(name: str) -> bool:
    return bool(_CLONE_DIR_RE.fullmatch(name or ""))


def is_reapable_checkout_dirname(name: str) -> bool:
    """Staged job UUID dirs or worker ``ai-review-<uuid>`` clone dirs."""
    return is_uuid_job_dirname(name) or is_clone_dirname(name)


def live_review_job_ids(client: DockerClient) -> set[str]:
    """Job ids from labeled review containers (any age / state)."""
    try:
        containers = client.list_review_containers()
    except DockerApiError as exc:
        logger.warning("reaper live job list failed: %s", exc)
        return set()
    live: set[str] = set()
    for item in containers:
        labels = item.get("Labels") or {}
        if labels.get(REVIEW_LABEL) != REVIEW_LABEL_VALUE and labels.get("opr.review") != "1":
            continue
        job_id = str(labels.get("opr.job_id") or "").strip()
        if job_id:
            live.add(job_id)
    return live


def _safe_unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _safe_rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def reap_checkout_orphans(
    *,
    base_dir: Path | None = None,
    ttl_seconds: int | None = None,
    live_job_ids: set[str] | None = None,
    client: DockerClient | None = None,
    now: float | None = None,
) -> int:
    """Remove orphan UUID / clone dirs under the checkout root. Returns removals count.

    Lease policy (primary): a directory with a fresh ``.lease`` is kept; stale
    lease (or no lease past the conservative age bound) may be removed.
    ``ttl_seconds`` is accepted for back-compat tests as an override of the
    no-lease age bound only (injected via env for the call). Symlinks are never
    followed (``lstat``); removals stay lexically under the resolved root.
    """
    base = (base_dir or checkout_base_path()).resolve()
    owns = False
    docker = client
    if live_job_ids is None:
        owns = docker is None
        docker = docker or DockerClient()
        try:
            live = live_review_job_ids(docker)
        finally:
            if owns and docker is not None:
                docker.close()
    else:
        live = live_job_ids

    live_norm = {j.replace("-", "").lower() for j in live}

    if not base.is_dir():
        return 0

    # Optional override of no-lease age for unit tests that pass ttl_seconds=.
    ttl_override = None if ttl_seconds is None else int(ttl_seconds)

    removed = 0
    clock = time.time() if now is None else float(now)
    try:
        names = os.listdir(base)
    except OSError as exc:
        logger.warning("reaper checkout list failed base=%s: %s", base, exc)
        return 0

    for name in names:
        if not is_reapable_checkout_dirname(name):
            continue
        # Live review containers still protect staged UUID job dirs.
        if is_uuid_job_dirname(name) and name.replace("-", "").lower() in live_norm:
            continue

        path = base / name
        try:
            st = path.lstat()
        except OSError:
            continue

        # Must stay inside root (lexical for symlinks: only remove the entry).
        try:
            if path.is_symlink():
                # Lease does not apply to symlink entries; age via lstat.
                age = clock - float(st.st_mtime)
                bound = ttl_override if ttl_override is not None else 60
                if age < bound:
                    continue
                logger.warning(
                    "reaping orphan checkout symlink name=%s age_s=%s",
                    name,
                    int(age),
                )
                _safe_unlink(path)
                removed += 1
                continue
            resolved = path.resolve(strict=False)
            resolved.relative_to(base)
        except (OSError, ValueError):
            logger.warning("skipping checkout entry outside root name=%s", name)
            continue

        if ttl_override is not None:
            # Test override: treat like legacy mtime TTL when no lease file,
            # but still honour a fresh lease as keep.
            from app.launcher.lease import lease_mtime, lease_stale_after_seconds

            mtime = lease_mtime(path)
            if mtime is not None:
                if (clock - mtime) <= float(lease_stale_after_seconds()):
                    continue
            else:
                if (clock - float(st.st_mtime)) < ttl_override:
                    continue
        elif not directory_is_lease_stale(path, now=clock):
            continue

        if stat.S_ISDIR(st.st_mode):
            logger.warning("reaping orphan checkout dir name=%s", name)
            _safe_rmtree(path)
            removed += 1
        elif stat.S_ISREG(st.st_mode):
            logger.warning("reaping orphan checkout file name=%s", name)
            _safe_unlink(path)
            removed += 1
    return removed


def reap_once(client: DockerClient | None = None) -> int:
    """Force-remove stale review containers; then stale checkout dirs."""
    ttl = orphan_ttl_seconds()
    owns = client is None
    docker = client or DockerClient()
    removed = 0
    try:
        try:
            containers = docker.list_review_containers()
        except DockerApiError as exc:
            logger.warning("reaper list failed: %s", exc)
            containers = []
        now = time.time()
        live_ids: set[str] = set()
        for item in containers:
            labels = item.get("Labels") or {}
            if labels.get(REVIEW_LABEL) != REVIEW_LABEL_VALUE and labels.get("opr.review") != "1":
                continue
            job_id = str(labels.get("opr.job_id") or "").strip()
            if job_id:
                live_ids.add(job_id)
            # Never reap containers whose launch is still active in this process.
            if job_id and lifecycle.launch_active(job_id):
                continue
            created = item.get("Created") or 0
            try:
                age = now - float(created)
            except (TypeError, ValueError):
                age = ttl + 1
            cid = item.get("Id") or ""
            if age < ttl:
                continue
            logger.warning(
                "reaping orphan review container id=%s age_s=%s names=%s",
                cid[:12],
                int(age),
                item.get("Names"),
            )
            try:
                docker.kill_container(cid)
            except DockerApiError:
                pass
            try:
                docker.remove_container(cid, force=True)
                removed += 1
                live_ids.discard(job_id)
            except DockerApiError as exc:
                logger.warning("reaper remove failed %s: %s", cid[:12], exc)

        removed += reap_checkout_orphans(live_job_ids=live_ids, client=docker)
        return removed
    finally:
        if owns:
            docker.close()


async def reaper_loop(stop: asyncio.Event) -> None:
    """Background task: reap on start, then every interval until stop is set."""
    try:
        await asyncio.to_thread(reap_once)
    except Exception:  # noqa: BLE001 - never crash the launcher on reaper errors
        logger.exception("initial reaper pass failed")
    interval = reaper_interval_seconds()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            try:
                await asyncio.to_thread(reap_once)
            except Exception:  # noqa: BLE001
                logger.exception("periodic reaper pass failed")
