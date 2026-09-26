"""Prometheus metrics and a small ops snapshot for workers / review runs."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import ReviewRun

logger = logging.getLogger(__name__)

REVIEW_RUNS = Counter(
    "open_pr_review_review_runs_total",
    "Finished review runs",
    ["status", "error_code", "trigger"],
)
REVIEW_DURATION = Histogram(
    "open_pr_review_review_duration_seconds",
    "End-to-end review duration",
    ["status"],
    buckets=(5, 15, 30, 60, 120, 180, 300, 600, 900, 1200),
)
CODEX_ATTEMPTS = Counter(
    "open_pr_review_codex_attempts_total",
    "Codex CLI invocations",
    ["result"],
)
CODEX_DURATION = Histogram(
    "open_pr_review_codex_duration_seconds",
    "Codex CLI wall time",
    ["result"],
    buckets=(5, 15, 30, 60, 120, 180, 300, 600, 900),
)
ENQUEUE_TOTAL = Counter(
    "open_pr_review_review_enqueue_total",
    "Queue outcomes for a review request",
    ["result"],
)
WORKER_JOBS_IN_PROGRESS = Gauge(
    "open_pr_review_worker_jobs_in_progress",
    "Review jobs executing in this worker process",
)
WORKER_UP = Gauge("open_pr_review_worker_up", "1 while this worker process is running")
IN_FLIGHT = Gauge(
    "open_pr_review_review_in_flight",
    "review_runs in pending or running",
    ["status"],
)
STALE_IN_FLIGHT = Gauge(
    "open_pr_review_review_stale_in_flight",
    "pending/running review_runs older than stale_minutes",
)
QUEUE_JOBS = Gauge("open_pr_review_arq_queue_jobs", "Jobs waiting in the ARQ Redis queue")
LAST_SUCCESS = Gauge(
    "open_pr_review_review_last_success_timestamp",
    "Unix time of the last completed review_run",
)
RECENT_FAILURES = Gauge(
    "open_pr_review_review_recent_failures",
    "Failed review_runs in the last hour",
    ["error_code"],
)

_STALE_MINUTES = 15
_metrics_server_started = False
_recent_fail_codes: set[str] = set()


def start_worker_metrics_server(port: int) -> None:
    global _metrics_server_started
    if _metrics_server_started or port <= 0:
        return
    try:
        start_http_server(port, addr="0.0.0.0")
    except OSError:
        logger.exception("could not bind metrics port %s", port)
        return
    WORKER_UP.set(1)
    _metrics_server_started = True
    logger.info("worker metrics listening on 0.0.0.0:%s", port)


def record_enqueue(result: str) -> None:
    ENQUEUE_TOTAL.labels(result=result).inc()


def record_review_finished(*, status: str, error_code: str | None, trigger: str, duration_ms: int | None) -> None:
    code = error_code or "none"
    REVIEW_RUNS.labels(status=status, error_code=code, trigger=trigger or "unknown").inc()
    REVIEW_DURATION.labels(status=status).observe(max((duration_ms or 0) / 1000.0, 0.0))
    if status == "completed" and not error_code:
        LAST_SUCCESS.set(time.time())
    logger.info(
        "review_finished status=%s error_code=%s trigger=%s duration_ms=%s",
        status,
        code,
        trigger,
        duration_ms,
    )


def record_codex_attempt(*, result: str, duration_s: float) -> None:
    CODEX_ATTEMPTS.labels(result=result).inc()
    CODEX_DURATION.labels(result=result).observe(max(duration_s, 0.0))


async def arq_queue_depth(redis: Any) -> int:
    try:
        return int(await redis.zcard("arq:queue"))
    except Exception:
        try:
            return int(await redis.llen("arq:queue"))
        except Exception:
            return 0


async def collect_runtime_gauges(
    session_factory: async_sessionmaker, redis: Any, *, stale_minutes: int = _STALE_MINUTES
) -> None:
    global _recent_fail_codes
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_minutes)
    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    async with session_factory() as session:
        inflight_rows = (
            await session.execute(
                select(ReviewRun.status, func.count(ReviewRun.id))
                .where(ReviewRun.status.in_(("pending", "running")))
                .group_by(ReviewRun.status)
            )
        ).all()
        stale = (
            await session.execute(
                select(func.count(ReviewRun.id)).where(
                    ReviewRun.status.in_(("pending", "running")), ReviewRun.updated_at < cutoff
                )
            )
        ).scalar_one()
        last_ok = (
            await session.execute(
                select(func.max(ReviewRun.updated_at)).where(
                    ReviewRun.status == "completed", ReviewRun.error_code.is_(None)
                )
            )
        ).scalar_one_or_none()
        fail_rows = (
            await session.execute(
                select(ReviewRun.error_code, func.count(ReviewRun.id))
                .where(ReviewRun.status == "failed", ReviewRun.updated_at >= hour_ago)
                .group_by(ReviewRun.error_code)
            )
        ).all()

    IN_FLIGHT.labels(status="pending").set(0)
    IN_FLIGHT.labels(status="running").set(0)
    for status, count in inflight_rows:
        IN_FLIGHT.labels(status=status).set(int(count))
    STALE_IN_FLIGHT.set(int(stale or 0))
    if last_ok is not None:
        LAST_SUCCESS.set(last_ok.timestamp())
    for code in _recent_fail_codes:
        RECENT_FAILURES.labels(error_code=code).set(0)
    _recent_fail_codes = set()
    for code, count in fail_rows:
        label = code or "none"
        RECENT_FAILURES.labels(error_code=label).set(int(count))
        _recent_fail_codes.add(label)
    QUEUE_JOBS.set(await arq_queue_depth(redis))


async def ops_snapshot(
    session_factory: async_sessionmaker, redis: Any, *, stale_minutes: int = _STALE_MINUTES
) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_minutes)
    async with session_factory() as session:
        inflight_rows = (
            await session.execute(
                select(ReviewRun.status, func.count(ReviewRun.id))
                .where(ReviewRun.status.in_(("pending", "running")))
                .group_by(ReviewRun.status)
            )
        ).all()
        stale_runs = (
            (
                await session.execute(
                    select(ReviewRun)
                    .where(ReviewRun.status.in_(("pending", "running")), ReviewRun.updated_at < cutoff)
                    .order_by(ReviewRun.updated_at.asc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        last_failures = (
            (
                await session.execute(
                    select(ReviewRun)
                    .where(ReviewRun.status == "failed")
                    .order_by(ReviewRun.updated_at.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        last_ok = (
            await session.execute(
                select(ReviewRun).where(ReviewRun.status == "completed").order_by(ReviewRun.updated_at.desc()).limit(1)
            )
        ).scalar_one_or_none()

    in_flight = {"pending": 0, "running": 0}
    for status, count in inflight_rows:
        in_flight[status] = int(count)
    return {
        "in_flight": in_flight,
        "queue_jobs": await arq_queue_depth(redis),
        "stale_minutes": stale_minutes,
        "stale_runs": [_run_brief(item) for item in stale_runs],
        "last_completed": _run_brief(last_ok) if last_ok else None,
        "last_failures": [_run_brief(item) for item in last_failures],
        "hints": _hints(in_flight, stale_runs, last_failures),
    }


def _run_brief(run: ReviewRun) -> dict[str, Any]:
    return {
        "review_run_id": str(run.id),
        "status": run.status,
        "error_code": run.error_code,
        "trigger": run.trigger,
        "head_sha": run.head_sha[:12] if run.head_sha else None,
        "duration_ms": run.duration_ms,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


def _hints(in_flight: dict[str, int], stale_runs: list[ReviewRun], last_failures: list[ReviewRun]) -> list[str]:
    hints: list[str] = []
    fail_codes = [item.error_code for item in last_failures if item.error_code]
    if "CODEX_QUOTA" in fail_codes:
        hints.append("OpenAI quota is empty (CODEX_QUOTA). Top up billing; reviews will fail until credits exist.")
    if "CODEX_AUTH" in fail_codes:
        hints.append("Codex has no usable API key (CODEX_AUTH). Check OPENAI_API_KEY / CODEX_API_KEY.")
    if stale_runs:
        hints.append(
            f"{len(stale_runs)} review_run(s) stuck in pending/running for >{_STALE_MINUTES}m. "
            "Worker may be down or a job is wedged."
        )
    if in_flight["pending"] and not in_flight["running"]:
        hints.append("Jobs are queued but none are running. Worker is not consuming the ARQ queue.")
    if not hints:
        hints.append("No obvious worker stall from the last failures / in-flight snapshot.")
    return hints
