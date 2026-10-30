"""Prometheus metrics for model-proxy (no secret labels)."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

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
TOKEN_REVOKE = Counter(
    "model_proxy_token_revoke_total",
    "Job token revoke outcomes",
    ["result"],
)
ACTIVE_JOBS = Gauge(
    "model_proxy_active_jobs",
    "Active (non-expired) job tokens",
)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
