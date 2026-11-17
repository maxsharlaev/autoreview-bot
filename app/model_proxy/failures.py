"""Bounded classification of model-proxy upstream failures.

HTTP status stays 504 for deadlines and 502 for transport. The JSON ``error``
token is what Codex may echo into the session log; ``kind`` is the precise cause.
Neither field ever contains a URL, token, or response body.
"""

from __future__ import annotations

import json
import ssl

import httpx

# Stable tokens. classify_codex_error matches these substrings.
ERROR_CONNECT_TIMEOUT = "upstream_connect_timeout"
ERROR_POOL_TIMEOUT = "upstream_pool_timeout"
ERROR_READ_TIMEOUT = "upstream_read_timeout"
ERROR_WRITE_TIMEOUT = "upstream_write_timeout"
ERROR_TIMEOUT = "upstream_timeout"
ERROR_IDLE = "idle_timeout"
ERROR_DNS = "upstream_dns"
ERROR_TLS = "upstream_tls"
ERROR_CONNECT = "upstream_connect"
ERROR_RESET = "upstream_reset"
ERROR_PROTOCOL = "upstream_protocol"
ERROR_TRANSPORT = "upstream_error"
ERROR_STREAM = "stream_disconnect"

_DNS_NEEDLES = (
    "getaddrinfo",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "name resolution",
)
_SECRET_NEEDLES = ("sk-", "bearer ", "api_key", "authorization")


def error_body(error: str, kind: str) -> bytes:
    return json.dumps({"error": error, "kind": kind}).encode()


def _chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(6):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _safe_text(exc: BaseException) -> str:
    text = str(exc).lower()[:300]
    for needle in _SECRET_NEEDLES:
        text = text.replace(needle, "")
    return text


def _is_dns(exc: BaseException) -> bool:
    for item in _chain(exc):
        name = type(item).__name__.lower()
        if "gaierror" in name or name == "herror":
            return True
        text = _safe_text(item)
        if any(needle in text for needle in _DNS_NEEDLES):
            return True
    return False


def _is_tls(exc: BaseException) -> bool:
    return any(isinstance(item, ssl.SSLError) for item in _chain(exc))


def classify_transport(exc: BaseException) -> tuple[str, str, int, str]:
    """Return ``(error, kind, http_status, metric_kind)`` for a transport failure.

    ``metric_kind`` is the ``model_proxy_upstream_errors_total`` label.
    """
    if isinstance(exc, TimeoutError) and not isinstance(exc, httpx.HTTPError):
        kind_name = str(exc) or "overall"
        if kind_name == "idle":
            return ERROR_IDLE, "idle_timeout", 504, "idle_timeout"
        return ERROR_TIMEOUT, "overall_timeout", 504, "timeout"
    if isinstance(exc, httpx.ConnectTimeout):
        return ERROR_CONNECT_TIMEOUT, "connect_timeout", 504, "connect_timeout"
    if isinstance(exc, httpx.PoolTimeout):
        return ERROR_POOL_TIMEOUT, "pool_timeout", 504, "pool_timeout"
    if isinstance(exc, httpx.WriteTimeout):
        return ERROR_WRITE_TIMEOUT, "write_timeout", 504, "write_timeout"
    if isinstance(exc, httpx.ReadTimeout):
        return ERROR_READ_TIMEOUT, "read_timeout", 504, "read_timeout"
    if isinstance(exc, httpx.TimeoutException):
        return ERROR_TIMEOUT, "timeout", 504, "timeout"
    if isinstance(exc, httpx.ConnectError):
        if _is_tls(exc):
            return ERROR_TLS, "tls", 502, "tls"
        if _is_dns(exc):
            return ERROR_DNS, "dns", 502, "dns"
        return ERROR_CONNECT, "connect", 502, "connect"
    if isinstance(
        exc,
        (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, ConnectionResetError, BrokenPipeError),
    ):
        return ERROR_RESET, "connection_reset", 502, "connection_reset"
    if isinstance(exc, httpx.ProtocolError):
        return ERROR_PROTOCOL, "protocol", 502, "protocol"
    return ERROR_TRANSPORT, "unknown_transport", 502, "http_error"


def classify_upstream_status(status: int, body: bytes = b"") -> tuple[str, str]:
    """Return ``(result_label, attempt_outcome)`` for a completed upstream HTTP status.

    ``attempt_outcome`` is empty when this response is not a real model attempt
    (unused today for 1xx). A completed 2xx/3xx/4xx other than 429 is ``ok`` only
    for statuses below 400. 401 is connectivity, not model success.
    """
    if status < 400:
        return "ok", "ok"
    if status in (401, 403):
        return "upstream_auth", "upstream_auth"
    if status == 429:
        low = body.lower()
        if b"insufficient_quota" in low or b"exceeded your current quota" in low:
            return "upstream_quota", "quota"
        return "upstream_rate_limit", "rate_limit"
    if status >= 500:
        return "upstream_5xx", "upstream_5xx"
    return "upstream_status", ""
