"""Prometheus metrics for model-proxy (no secret labels)."""

from __future__ import annotations

import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUESTS = Counter(
    "model_proxy_requests_total",
    "Proxied or rejected model-API requests",
    ["result"],
)
AUTH_FAIL = Counter(
    "model_proxy_auth_fail_total",
    "Authentication failures",
    ["kind"],
)
ALLOWLIST_DENY = Counter(
    "model_proxy_allowlist_deny_total",
    "Requests rejected by method/path/upgrade allowlist",
    ["reason"],
)
UPSTREAM_ERRORS = Counter(
    "model_proxy_upstream_errors_total",
    "Upstream provider errors",
    ["kind"],
)
BYTES = Counter(
    "model_proxy_bytes_total",
    "Bytes transferred through the proxy",
    ["direction"],
)
TOKEN_ISSUE = Counter(
    "model_proxy_token_issue_total",
    "Job token issue outcomes",
    ["result"],
)
ACTIVE_JOBS = Gauge(
    "model_proxy_active_jobs",
    "Active (non-expired) job tokens",
)
DURATION = Histogram(
    "model_proxy_request_duration_seconds",
    "Terminal model-proxy attempt duration",
    ["result"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
)
MODEL_ATTEMPTS = Counter(
    "model_proxy_model_attempts_total",
    "Real upstream model attempts by terminal outcome",
    ["outcome"],
)
CONSECUTIVE_UPSTREAM_FAILURES = Gauge(
    "model_proxy_consecutive_upstream_failures",
    "Consecutive timeout, transport, 5xx, or broken-stream attempts. 0 after a real success or process restart.",
)
LAST_UPSTREAM_SUCCESS = Gauge(
    "model_proxy_last_upstream_success_timestamp_seconds",
    "Unix time of the last upstream status below 400. Stays 0 until one is observed.",
)

# outcome label -> counts toward the connectivity-failure streak
_FAILURE_OUTCOMES = frozenset({"timeout", "transport", "upstream_5xx", "stream_failed"})
_ATTEMPT_OUTCOME = {
    "ok": "ok",
    "upstream_timeout": "timeout",
    "upstream_connect_timeout": "timeout",
    "upstream_pool_timeout": "timeout",
    "upstream_read_timeout": "timeout",
    "upstream_write_timeout": "timeout",
    "idle_timeout": "timeout",
    "upstream_dns": "transport",
    "upstream_tls": "transport",
    "upstream_connect": "transport",
    "upstream_reset": "transport",
    "upstream_protocol": "transport",
    "upstream_error": "transport",
    "stream_disconnect": "stream_failed",
    "upstream_5xx": "upstream_5xx",
    "upstream_rate_limit": "rate_limit",
    "upstream_quota": "quota",
    "upstream_auth": "upstream_auth",
}
_consecutive = 0


def reset_model_health_for_tests() -> None:
    global _consecutive
    _consecutive = 0
    CONSECUTIVE_UPSTREAM_FAILURES.set(0)
    LAST_UPSTREAM_SUCCESS.set(0)


def note_model_attempt(result: str, duration_s: float) -> None:
    """Count one terminal attempt. Does not include job-token auth or allowlist rejects."""
    global _consecutive
    REQUESTS.labels(result=result).inc()
    DURATION.labels(result=result).observe(max(0.0, float(duration_s)))
    outcome = _ATTEMPT_OUTCOME.get(result, "")
    if not outcome:
        return
    MODEL_ATTEMPTS.labels(outcome=outcome).inc()
    if outcome in _FAILURE_OUTCOMES:
        _consecutive += 1
    elif outcome == "ok":
        _consecutive = 0
        LAST_UPSTREAM_SUCCESS.set(time.time())
    CONSECUTIVE_UPSTREAM_FAILURES.set(_consecutive)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
