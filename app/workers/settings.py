from __future__ import annotations

import logging
import uuid

from arq.cron import cron

from app.adapters.slack import SlackClient
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
    _load_owner_config()
    start_worker_metrics_server(settings.metrics_port)
    engine = create_engine()
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    logger.info("worker ready max_jobs=%s job_timeout=%ss", settings.worker_max_jobs, settings.worker_job_timeout)


def _load_owner_config() -> None:
    """Validate the owner config like API startup: a config error stops the worker (exit 1).

    Missing legacy credentials are not an error (mode A without credentials builds an
    empty registry). Also emits the legacy 'default' binding warning once per worker start.
    """
    from app.owners.registry import OwnerConfigError, get_owner_registry, warn_if_legacy_default_unbound

    try:
        registry = get_owner_registry()
    except OwnerConfigError as exc:
        logger.error("Owner configuration error: %s", exc)
        raise SystemExit(1) from exc
    warn_if_legacy_default_unbound(registry, logger)


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
    if not config.schedule.review_digest.enabled:
        return "disabled"

    from app.owners.registry import get_owner_registry

    registry = get_owner_registry()
    # features.digest_enabled is per owner (effective config); the schedule stays global.
    digest_owners = [
        (owner_id, owner_ctx)
        for owner_id, owner_ctx in registry.owners.items()
        if owner_ctx.config.features.digest_enabled
    ]
    if registry.owners and not digest_owners:
        return "disabled"
    if not registry.owners and not config.features.digest_enabled:
        return "disabled"
    factory = ctx["session_factory"]

    # Run digest for each owner with their own credentials
    digest_ids: list[str] = []
    for owner_id, owner_ctx in digest_owners:
        try:
            from app.adapters.github import GitHubAppClient

            github = GitHubAppClient.from_credentials(owner_ctx.github, owner_id=owner_id)
        except Exception:
            logger.exception("GitHub client unavailable for digest refresh, owner=%s", owner_id)
            continue

        # Get owner's specific allowed_repos (None for legacy owner means use global config)
        owner_allowed_repos = registry.get_allowed_repos_for_owner(owner_id)
        if owner_allowed_repos is None:
            # Legacy default owner: use global config's allowed_repos
            owner_allowed_repos = list(config.github.allowed_repos)

        # Each owner posts only with its own Slack binding; no binding -> never posts.
        slack = (
            SlackClient.from_binding(owner_ctx.slack, owner_ctx.config)
            if owner_ctx.slack
            else SlackClient.disabled(owner_ctx.config)
        )

        async with factory() as session:
            digest = await run_digest(
                session,
                config=owner_ctx.config,
                slack=slack,
                github=github,
                owner_id=owner_id,
                allowed_repos=owner_allowed_repos,
                legacy_default_owner=registry.legacy_default_alias(),
            )
            digest_ids.append(str(digest.id))

    return ",".join(digest_ids) if digest_ids else "no_owners"


def _digest_enabled_for_any_owner(config) -> bool:
    """True if some active owner has features.digest_enabled in its effective config."""
    from app.owners.registry import get_owner_registry

    # No fallback on errors: an invalid owner config must fail the worker, not silently
    # decide whether the digest cron is registered.
    registry = get_owner_registry()
    if not registry.owners:
        return config.features.digest_enabled
    return any(owner_ctx.config.features.digest_enabled for owner_ctx in registry.owners.values())


def _cron_jobs() -> list:
    config = get_app_config()
    jobs = []
    if config.schedule.review_digest.enabled and _digest_enabled_for_any_owner(config):
        jobs.append(cron(digest_open_prs, **parse_cron(config.schedule.review_digest.cron)))
    return jobs


class WorkerSettings:
    functions = [review_pull_request, digest_open_prs]
    cron_jobs = _cron_jobs()
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    max_jobs = get_settings().worker_max_jobs
    job_timeout = get_settings().worker_job_timeout
