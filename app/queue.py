from __future__ import annotations

import asyncio
import logging

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.jobs import Job, JobStatus

from app.config import get_settings

logger = logging.getLogger(__name__)

# Redis calls made while enqueueing are bounded so a slow Redis cannot stall webhook/API requests.
ABORT_TIMEOUT_SECONDS = 2.0
JOB_STATUS_TIMEOUT_SECONDS = 2.0


def redis_settings(url: str | None = None) -> RedisSettings:
    return RedisSettings.from_dsn(url or get_settings().redis_url)


async def create_redis_pool(url: str | None = None) -> ArqRedis:
    return await create_pool(redis_settings(url))


def review_job_id(review_run_id: str) -> str:
    """Deterministic arq job id for a run: stored before enqueueing, and arq refuses duplicates."""
    return f"review:{review_run_id}"


async def enqueue_review(redis: ArqRedis, review_run_id: str) -> str | None:
    job_id = review_job_id(review_run_id)
    job = await redis.enqueue_job("review_pull_request", review_run_id, _job_id=job_id)
    return job.job_id if job else job_id


async def abort_job(redis: ArqRedis, job_id: str | None, timeout: float | None = None) -> None:
    """Best-effort abort request; waits at most `timeout` seconds and never raises."""
    if not job_id:
        return
    timeout = ABORT_TIMEOUT_SECONDS if timeout is None else timeout
    job = Job(job_id, redis)
    try:
        # Job.abort polls until the worker reacts; the outer bound also covers a hung Redis call.
        await asyncio.wait_for(job.abort(timeout=timeout, poll_delay=min(0.5, timeout)), timeout=timeout + 0.5)
    except Exception as exc:
        logger.info("abort of job %s not confirmed (ignored): %s", job_id, exc or type(exc).__name__)


async def job_is_active(redis: ArqRedis, job_id: str | None) -> bool:
    if not job_id:
        return False
    try:
        status = await asyncio.wait_for(Job(job_id, redis).status(), timeout=JOB_STATUS_TIMEOUT_SECONDS)
    except Exception:
        return False
    return status in {JobStatus.deferred, JobStatus.queued, JobStatus.in_progress}
