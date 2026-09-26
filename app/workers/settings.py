from __future__ import annotations

import logging
import uuid

from arq.cron import cron

from app.config import get_app_config, get_settings
from app.db import create_engine, create_session_factory
from app.logging_setup import configure_logging
from app.metrics import WORKER_JOBS_IN_PROGRESS, WORKER_UP, record_review_finished, start_worker_metrics_server
from app.models import ReviewRun
from app.queue import redis_settings
from app.services.constants import INTERNAL_ERROR
from app.services.digest import run_digest
from app.services.orchestrator import run_review

logger = logging.getLogger(__name__)


def parse_cron(expr: str) -> dict[str, set[int]]:
    parts = (expr or "0 * * * *").split()
    if len(parts) != 5:
        return {"minute": {0}}
    minute, hour, *_rest = parts
    kwargs: dict[str, set[int]] = {}
    if minute.isdigit():
        kwargs["minute"] = {int(minute)}
    else:
        kwargs["minute"] = {0}
    if hour.isdigit():
        kwargs["hour"] = {int(hour)}
    return kwargs


async def startup(ctx: dict) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    start_worker_metrics_server(settings.metrics_port)
    engine = create_engine()
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    logger.info("worker ready max_jobs=%s job_timeout=%ss", settings.worker_max_jobs, settings.worker_job_timeout)


async def shutdown(ctx: dict) -> None:
    WORKER_UP.set(0)
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()


async def review_pull_request(ctx: dict, review_run_id: str) -> str:
    factory = ctx["session_factory"]
    run_id = uuid.UUID(review_run_id)
    logger.info("job start review_run_id=%s", review_run_id)
    WORKER_JOBS_IN_PROGRESS.inc()
    try:
        async with factory() as session:
            try:
                run = await run_review(session, run_id)
                logger.info("job done review_run_id=%s status=%s", review_run_id, run.status)
                return run.status
            except Exception:
                logger.exception("review_pull_request crashed for %s", review_run_id)
                await session.rollback()
                run = await session.get(ReviewRun, run_id)
                if run is not None and run.status in {"pending", "running"}:
                    run.status = "failed"
                    run.error_code = INTERNAL_ERROR
                    await session.commit()
                    record_review_finished(
                        status="failed",
                        error_code=INTERNAL_ERROR,
                        trigger=run.trigger,
                        duration_ms=run.duration_ms,
                    )
                raise
    finally:
        WORKER_JOBS_IN_PROGRESS.dec()


async def digest_open_prs(ctx: dict) -> str:
    config = get_app_config()
    if not config.features.digest_enabled or not config.schedule.review_digest.enabled:
        return "disabled"
    factory = ctx["session_factory"]
    github = None
    try:
        from app.adapters.github import GitHubAppClient

        github = GitHubAppClient()
    except Exception:
        logger.exception("GitHub client unavailable for digest refresh")
    async with factory() as session:
        digest = await run_digest(session, config=config, github=github)
        return str(digest.id)


def _cron_jobs() -> list:
    config = get_app_config()
    if not (config.features.digest_enabled and config.schedule.review_digest.enabled):
        return []
    return [cron(digest_open_prs, **parse_cron(config.schedule.review_digest.cron))]


class WorkerSettings:
    functions = [review_pull_request, digest_open_prs]
    cron_jobs = _cron_jobs()
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    max_jobs = get_settings().worker_max_jobs
    job_timeout = get_settings().worker_job_timeout
