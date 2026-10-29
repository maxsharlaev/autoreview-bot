"""Remove orphaned review containers and staged checkout job dirs."""

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
from app.launcher.spec import REVIEW_LABEL, REVIEW_LABEL_VALUE
from app.launcher.timeouts import checkout_orphan_ttl_seconds

logger = logging.getLogger(__name__)

# uuid.uuid4().hex (32) or canonical dashed UUID.
_UUID_DIR_RE = re.compile(
    r"^(?:"
    r"[0-9a-f]{32}"
    r"|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r")$",
    re.IGNORECASE,
)


def orphan_ttl_seconds() -> int:
    raw = os.environ.get("OPR_REVIEW_ORPHAN_TTL_SECONDS", "7200").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 7200


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
    # Do not follow symlinks: rmtree follows dir symlinks on some platforms when
    # the top entry is a real dir; we only call this after confirming not a symlink.
    shutil.rmtree(path, ignore_errors=True)


def reap_checkout_orphans(
    *,
    base_dir: Path | None = None,
    ttl_seconds: int | None = None,
    live_job_ids: set[str] | None = None,
    client: DockerClient | None = None,
) -> int:
    """Remove orphan UUID job dirs under the checkout root. Returns removals count.

    Only top-level names that are strict UUIDs are considered. Symlinks are never
    followed (``lstat``); a UUID-named symlink is unlinked as an entry. Removals
    stay lexically under the resolved checkout root.
    """
    base = (base_dir or checkout_base_path()).resolve()
    ttl = checkout_orphan_ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
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

    removed = 0
    now = time.time()
    try:
        names = os.listdir(base)
    except OSError as exc:
        logger.warning("reaper checkout list failed base=%s: %s", base, exc)
        return 0

    for name in names:
        if not is_uuid_job_dirname(name):
            continue
        if name.replace("-", "").lower() in live_norm:
            continue

        path = base / name
        try:
            st = path.lstat()
        except OSError:
            continue
        age = now - st.st_mtime
        if age < ttl:
            continue

        # Must stay inside root (lexical for symlinks: only remove the entry).
        try:
            if path.is_symlink():
                # Entry only — never follow.
                logger.warning(
                    "reaping orphan checkout symlink name=%s age_s=%s",
                    name,
                    int(age),
                )
                _safe_unlink(path)
                removed += 1
                continue
            # Real path must remain under base after resolve without escaping.
            resolved = path.resolve(strict=False)
            resolved.relative_to(base)
        except (OSError, ValueError):
            logger.warning("skipping checkout entry outside root name=%s", name)
            continue

        if stat.S_ISDIR(st.st_mode):
            logger.warning("reaping orphan checkout dir name=%s age_s=%s", name, int(age))
            _safe_rmtree(path)
            removed += 1
        elif stat.S_ISREG(st.st_mode):
            logger.warning("reaping orphan checkout file name=%s age_s=%s", name, int(age))
            _safe_unlink(path)
            removed += 1
    return removed


def reap_once(client: DockerClient | None = None) -> int:
    """Force-remove review containers older than TTL; then stale checkout dirs."""
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
