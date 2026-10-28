"""Remove orphaned review containers (label opr.review=1) on start and periodically."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from app.launcher.docker_api import DockerApiError, DockerClient
from app.launcher.spec import REVIEW_LABEL

logger = logging.getLogger(__name__)


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


def reap_once(client: DockerClient | None = None) -> int:
    """Force-remove review containers older than TTL. Returns number removed."""
    ttl = orphan_ttl_seconds()
    owns = client is None
    docker = client or DockerClient()
    removed = 0
    try:
        try:
            containers = docker.list_review_containers()
        except DockerApiError as exc:
            logger.warning("reaper list failed: %s", exc)
            return 0
        now = time.time()
        for item in containers:
            created = item.get("Created") or 0
            try:
                age = now - float(created)
            except (TypeError, ValueError):
                age = ttl + 1
            cid = item.get("Id") or ""
            labels = item.get("Labels") or {}
            if labels.get(REVIEW_LABEL) != "1" and labels.get("opr.review") != "1":
                continue
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
            except DockerApiError as exc:
                logger.warning("reaper remove failed %s: %s", cid[:12], exc)
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
