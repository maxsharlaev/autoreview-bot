"""Environment settings for the model-proxy service (fail closed)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

MIN_ISSUER_TOKEN_LEN = 16
DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS = 600
DEFAULT_TOKEN_GRACE_SECONDS = 60
DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_REQUESTS_PER_JOB = 50
DEFAULT_MAX_BYTES_PER_JOB = 16 * 1024 * 1024
DEFAULT_MAX_CONCURRENT_PER_JOB = 2
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 300
DEFAULT_UPSTREAM_HOST = "api.openai.com"
DEFAULT_UPSTREAM_SCHEME = "https"
DEFAULT_PROVIDER = "openai"
OWNER_KEY_RE = re.compile(r"^PROXY_OWNER_([A-Z0-9_]+)_OPENAI_API_KEY$")


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _float_env(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class ProxySettings:
    issuer_token: str
    default_upstream_key: str
    owner_upstream_keys: dict[str, str]
    upstream_host: str
    upstream_scheme: str
    provider: str
    max_review_timeout_seconds: int
    token_grace_seconds: int
    max_body_bytes: int
    max_requests_per_job: int
    max_bytes_per_job: int
    max_concurrent_per_job: int
    stream_idle_timeout_seconds: float
    upstream_timeout_seconds: float

    @property
    def max_token_ttl_seconds(self) -> int:
        return int(self.max_review_timeout_seconds) + int(self.token_grace_seconds)

    def upstream_key_for_owner(self, owner_id: str) -> str:
        oid = (owner_id or "").strip()
        if oid:
            key = self.owner_upstream_keys.get(oid) or self.owner_upstream_keys.get(oid.upper())
            if key:
                return key
        if not self.default_upstream_key:
            raise KeyError(f"no upstream model key for owner {oid or 'default'}")
        return self.default_upstream_key


def load_owner_upstream_keys() -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in os.environ.items():
        match = OWNER_KEY_RE.fullmatch(name)
        if not match:
            continue
        key = (value or "").strip()
        if key:
            out[match.group(1)] = key
    return out


def load_settings() -> ProxySettings:
    issuer = os.environ.get("PROXY_ISSUER_TOKEN", "").strip()
    default_key = os.environ.get("OPENAI_API_KEY", "").strip()
    owner_keys = load_owner_upstream_keys()
    upstream_host = (os.environ.get("PROXY_UPSTREAM_HOST", "").strip() or DEFAULT_UPSTREAM_HOST).lower()
    upstream_scheme = (os.environ.get("PROXY_UPSTREAM_SCHEME", "").strip() or DEFAULT_UPSTREAM_SCHEME).lower()
    provider = (os.environ.get("PROXY_PROVIDER", "").strip() or DEFAULT_PROVIDER).lower()
    max_review = _int_env("OPR_MAX_REVIEW_TIMEOUT_SECONDS", DEFAULT_MAX_REVIEW_TIMEOUT_SECONDS)
    grace = _int_env("PROXY_TOKEN_GRACE_SECONDS", DEFAULT_TOKEN_GRACE_SECONDS, minimum=0)
    max_body = _int_env("PROXY_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)
    max_reqs = _int_env("PROXY_MAX_REQUESTS_PER_JOB", DEFAULT_MAX_REQUESTS_PER_JOB)
    max_bytes = _int_env("PROXY_MAX_BYTES_PER_JOB", DEFAULT_MAX_BYTES_PER_JOB)
    max_conc = _int_env("PROXY_MAX_CONCURRENT_PER_JOB", DEFAULT_MAX_CONCURRENT_PER_JOB)
    idle = _float_env("PROXY_STREAM_IDLE_TIMEOUT_SECONDS", float(DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS))
    upstream_timeout = _float_env(
        "PROXY_UPSTREAM_TIMEOUT_SECONDS",
        float(max_review + grace),
        minimum=1.0,
    )
    return ProxySettings(
        issuer_token=issuer,
        default_upstream_key=default_key,
        owner_upstream_keys=owner_keys,
        upstream_host=upstream_host,
        upstream_scheme=upstream_scheme,
        provider=provider,
        max_review_timeout_seconds=max_review,
        token_grace_seconds=grace,
        max_body_bytes=max_body,
        max_requests_per_job=max_reqs,
        max_bytes_per_job=max_bytes,
        max_concurrent_per_job=max_conc,
        stream_idle_timeout_seconds=idle,
        upstream_timeout_seconds=upstream_timeout,
    )


def validate_settings(settings: ProxySettings | None = None) -> ProxySettings:
    """Fail closed when issuer token or upstream keys are missing / unsafe."""
    cfg = settings or load_settings()
    if len(cfg.issuer_token) < MIN_ISSUER_TOKEN_LEN:
        raise RuntimeError(
            f"PROXY_ISSUER_TOKEN missing or too short (need >= {MIN_ISSUER_TOKEN_LEN} chars); "
            "refusing to start model-proxy"
        )
    if not cfg.default_upstream_key and not cfg.owner_upstream_keys:
        raise RuntimeError("model-proxy needs OPENAI_API_KEY and/or PROXY_OWNER_<ID>_OPENAI_API_KEY; refusing to start")
    if cfg.upstream_scheme not in {"https", "http"}:
        raise RuntimeError(f"PROXY_UPSTREAM_SCHEME must be https or http, got {cfg.upstream_scheme!r}")
    host = cfg.upstream_host
    if not host or "/" in host or host.startswith(".") or " " in host:
        raise RuntimeError(f"invalid PROXY_UPSTREAM_HOST: {cfg.upstream_host!r}")
    if cfg.provider != "openai":
        raise RuntimeError(f"unsupported PROXY_PROVIDER {cfg.provider!r} (only openai in M3 PR2)")
    return cfg
