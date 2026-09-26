from __future__ import annotations

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.jobs import Job, JobStatus

from app.config import get_settings


def redis_settings(url: str | None = None) -> RedisSettings:
    return RedisSettings.from_dsn(url or get_settings().redis_url)


async def create_redis_pool(url: str | None = None) -> ArqRedis:
    return await create_pool(redis_settings(url))


async def enqueue_review(redis: ArqRedis, review_run_id: str) -> str | None:
    job = await redis.enqueue_job("review_pull_request", review_run_id)
    return job.job_id if job else None


async def abort_job(redis: ArqRedis, job_id: str | None) -> None:
    if not job_id:
        return
    job = Job(job_id, redis)
    try:
        await job.abort()
    except Exception:
        return


async def job_is_active(redis: ArqRedis, job_id: str | None) -> bool:
    if not job_id:
        return False
    try:
        status = await Job(job_id, redis).status()
    except Exception:
        return False
    return status in {JobStatus.deferred, JobStatus.queued, JobStatus.in_progress}
