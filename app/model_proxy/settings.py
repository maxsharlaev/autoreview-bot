"""Environment settings for the model-proxy service (fail closed)."""

from __future__ import annotations

import json
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
# Fragment inside PROXY_OWNER_<FRAG>_OPENAI_API_KEY / derived names.
_OWNER_FRAG_RE = re.compile(r"^[A-Z0-9_]+$")
# Env var names referenced by PROXY_OWNER_KEY_ENVS (no whitespace / '=').
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def derived_owner_key_env(owner_id: str) -> str:
    """Legacy env name ``PROXY_OWNER_<UPPER>_OPENAI_API_KEY`` for non-hyphen owner ids.

    Hyphenated owner ids cannot use this path (would collide with underscores);
    they require an explicit ``PROXY_OWNER_KEY_ENVS`` mapping.
    """
    oid = (owner_id or "").strip()
    if not oid:
        raise ValueError("empty owner id")
    if "-" in oid:
        raise ValueError(f"hyphenated owner id {oid!r} requires explicit PROXY_OWNER_KEY_ENVS mapping")
    frag = oid.upper()
    if not _OWNER_FRAG_RE.fullmatch(frag):
        raise ValueError(f"owner id {oid!r} is not valid for derived PROXY_OWNER_* env name")
    return f"PROXY_OWNER_{frag}_OPENAI_API_KEY"


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
    # Exact owner_id -> upstream API key value (from PROXY_OWNER_KEY_ENVS).
    owner_upstream_keys: dict[str, str]
    # UPPER fragment -> key from legacy PROXY_OWNER_<FRAG>_OPENAI_API_KEY scan.
    legacy_owner_upstream_keys: dict[str, str]
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
        if oid and oid in self.owner_upstream_keys:
            return self.owner_upstream_keys[oid]
        if oid and "-" in oid:
            # Hyphenated ids never use legacy derivation (ARB34-003).
            if not self.default_upstream_key:
                raise KeyError(f"no upstream model key for hyphenated owner {oid!r} (add PROXY_OWNER_KEY_ENVS mapping)")
            return self.default_upstream_key
        if oid:
            try:
                frag = oid.upper()
                if _OWNER_FRAG_RE.fullmatch(frag):
                    key = self.legacy_owner_upstream_keys.get(frag)
                    if key:
                        return key
            except Exception:  # noqa: BLE001
                pass
        if not self.default_upstream_key:
            raise KeyError(f"no upstream model key for owner {oid or 'default'}")
        return self.default_upstream_key


def load_owner_key_env_map() -> dict[str, str]:
    """Parse ``PROXY_OWNER_KEY_ENVS`` JSON: owner_id -> env var *name* (not secret).

    Empty / unset -> {}. Fail closed on invalid JSON or shape.
    """
    raw = os.environ.get("PROXY_OWNER_KEY_ENVS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"PROXY_OWNER_KEY_ENVS is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("PROXY_OWNER_KEY_ENVS must be a JSON object of owner_id -> env var name")
    out: dict[str, str] = {}
    for raw_oid, raw_env in data.items():
        if not isinstance(raw_oid, str) or not isinstance(raw_env, str):
            raise RuntimeError("PROXY_OWNER_KEY_ENVS keys and values must be strings")
        oid = raw_oid.strip()
        env_name = raw_env.strip()
        if not oid:
            raise RuntimeError("PROXY_OWNER_KEY_ENVS contains an empty owner id")
        if not env_name or not _ENV_NAME_RE.fullmatch(env_name):
            raise RuntimeError(f"PROXY_OWNER_KEY_ENVS owner {oid!r} has invalid env var name {raw_env!r}")
        if oid in out:
            raise RuntimeError(f"PROXY_OWNER_KEY_ENVS duplicates owner id {oid!r}")
        out[oid] = env_name
    return out


def load_legacy_owner_upstream_keys() -> dict[str, str]:
    """Scan ``PROXY_OWNER_<FRAG>_OPENAI_API_KEY`` -> UPPER fragment -> key value."""
    out: dict[str, str] = {}
    for name, value in os.environ.items():
        match = OWNER_KEY_RE.fullmatch(name)
        if not match:
            continue
        key = (value or "").strip()
        if not key:
            continue
        frag = match.group(1)
        existing = out.get(frag)
        if existing is not None and existing != key:
            raise RuntimeError(f"conflicting values for legacy env {name!r}; refuse to start")
        out[frag] = key
    return out


def resolve_owner_upstream_keys(explicit_map: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve explicit owner_id -> env-var-name map into owner_id -> key value.

    Startup validation (ARB34-003):
    - two owners must not map to the same env var name
    - each referenced env var must be present and non-empty
    - owner ids containing ``-`` are only allowed via this explicit map (callers
      that need hyphenated owners must list them here)
    - owner ids that differ only by case must not share a derived legacy name
      unless each has its own explicit env mapping (if both appear in the map
      with the same env name, the unique-env check fails; if both omit the map
      they would share ``PROXY_OWNER_<UPPER>_...`` — require explicit mapping
      when both case variants are declared in the map and would otherwise collide
      on the derived name with identical env targets already covered above)
    """
    explicit = explicit_map if explicit_map is not None else load_owner_key_env_map()

    env_to_owners: dict[str, list[str]] = {}
    for oid, env_name in explicit.items():
        env_to_owners.setdefault(env_name, []).append(oid)
    for env_name, owners in env_to_owners.items():
        if len(owners) > 1:
            raise RuntimeError(
                f"PROXY_OWNER_KEY_ENVS maps multiple owners {owners!r} to the same "
                f"env var {env_name!r}; refuse to start"
            )

    # Case-collision among map keys: variants that share upper() must each have
    # distinct env vars (already enforced) — additionally, if any variant relies
    # on the derived legacy name as its mapped target while another shares that
    # derived name, fail clearly.
    by_upper: dict[str, list[str]] = {}
    for oid in explicit:
        if "-" in oid:
            continue
        try:
            frag = oid.upper()
            if not _OWNER_FRAG_RE.fullmatch(frag):
                raise RuntimeError(f"PROXY_OWNER_KEY_ENVS owner id {oid!r} is not valid for key mapping")
        except RuntimeError:
            raise
        by_upper.setdefault(oid.upper(), []).append(oid)
    for frag, oids in by_upper.items():
        if len(oids) < 2:
            continue
        mapped_envs = {explicit[oid] for oid in oids}
        if len(mapped_envs) < len(oids):
            raise RuntimeError(
                f"case-colliding owner ids {oids!r} share env mapping for fragment {frag!r}; refuse to start"
            )

    out: dict[str, str] = {}
    for oid, env_name in explicit.items():
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise RuntimeError(
                f"PROXY_OWNER_KEY_ENVS owner {oid!r} references env {env_name!r} "
                "which is missing or empty; refuse to start"
            )
        out[oid] = value
    return out


def load_owner_upstream_keys() -> dict[str, str]:
    """Backward-compatible name: resolved explicit owner_id -> key map only."""
    return resolve_owner_upstream_keys()


def load_settings() -> ProxySettings:
    issuer = os.environ.get("PROXY_ISSUER_TOKEN", "").strip()
    default_key = os.environ.get("OPENAI_API_KEY", "").strip()
    owner_keys = resolve_owner_upstream_keys()
    legacy_keys = load_legacy_owner_upstream_keys()
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
        legacy_owner_upstream_keys=legacy_keys,
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
    # Re-resolve map so invalid PROXY_OWNER_KEY_ENVS fails closed at startup.
    resolve_owner_upstream_keys(load_owner_key_env_map())

    cfg = settings or load_settings()
    if len(cfg.issuer_token) < MIN_ISSUER_TOKEN_LEN:
        raise RuntimeError(
            f"PROXY_ISSUER_TOKEN missing or too short (need >= {MIN_ISSUER_TOKEN_LEN} chars); "
            "refusing to start model-proxy"
        )
    if not cfg.default_upstream_key and not cfg.owner_upstream_keys and not cfg.legacy_owner_upstream_keys:
        raise RuntimeError(
            "model-proxy needs OPENAI_API_KEY and/or PROXY_OWNER_<ID>_OPENAI_API_KEY "
            "and/or PROXY_OWNER_KEY_ENVS; refusing to start"
        )
    if cfg.upstream_scheme not in {"https", "http"}:
        raise RuntimeError(f"PROXY_UPSTREAM_SCHEME must be https or http, got {cfg.upstream_scheme!r}")
    host = cfg.upstream_host
    if not host or "/" in host or host.startswith(".") or " " in host:
        raise RuntimeError(f"invalid PROXY_UPSTREAM_HOST: {cfg.upstream_host!r}")
    if cfg.provider != "openai":
        raise RuntimeError(f"unsupported PROXY_PROVIDER {cfg.provider!r} (only openai in M3 PR2)")
    return cfg
