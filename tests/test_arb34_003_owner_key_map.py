"""ARB34-003: explicit PROXY_OWNER_KEY_ENVS map; no hyphen canonicalization."""

from __future__ import annotations

import json

import pytest
from app.model_proxy.settings import (
    ProxySettings,
    derived_owner_key_env,
    load_owner_key_env_map,
    load_settings,
    resolve_owner_upstream_keys,
    validate_settings,
)


def _base_settings(**overrides: object) -> ProxySettings:
    defaults: dict[str, object] = {
        "issuer_token": "x" * 32,
        "default_upstream_key": "sk-default",
        "owner_upstream_keys": {},
        "legacy_owner_upstream_keys": {},
        "upstream_host": "api.openai.com",
        "upstream_scheme": "https",
        "provider": "openai",
        "max_review_timeout_seconds": 600,
        "token_grace_seconds": 60,
        "max_body_bytes": 1024,
        "max_requests_per_job": 10,
        "max_bytes_per_job": 1024,
        "max_concurrent_per_job": 2,
        "stream_idle_timeout_seconds": 30.0,
        "upstream_timeout_seconds": 60.0,
    }
    defaults.update(overrides)
    return ProxySettings(**defaults)  # type: ignore[arg-type]


def test_derived_owner_key_env_rejects_hyphen() -> None:
    with pytest.raises(ValueError, match="hyphenated"):
        derived_owner_key_env("org-a")
    assert derived_owner_key_env("org_a") == "PROXY_OWNER_ORG_A_OPENAI_API_KEY"
    assert derived_owner_key_env("ACME") == "PROXY_OWNER_ACME_OPENAI_API_KEY"


def test_org_hyphen_and_underscore_route_to_distinct_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORG_A_HYPHEN_KEY", "sk-hyphen")
    monkeypatch.setenv("ORG_A_UNDER_KEY", "sk-under")
    mapping = {"org-a": "ORG_A_HYPHEN_KEY", "org_a": "ORG_A_UNDER_KEY"}
    monkeypatch.setenv("PROXY_OWNER_KEY_ENVS", json.dumps(mapping))
    resolved = resolve_owner_upstream_keys()
    assert resolved["org-a"] == "sk-hyphen"
    assert resolved["org_a"] == "sk-under"
    settings = _base_settings(owner_upstream_keys=resolved, default_upstream_key="sk-default")
    assert settings.upstream_key_for_owner("org-a") == "sk-hyphen"
    assert settings.upstream_key_for_owner("org_a") == "sk-under"
    # Must not silently fall back to default when mapped
    assert settings.upstream_key_for_owner("org-a") != "sk-default"


def test_collision_same_env_var_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARED_KEY", "sk-shared")
    monkeypatch.setenv(
        "PROXY_OWNER_KEY_ENVS",
        json.dumps({"org-a": "SHARED_KEY", "org_a": "SHARED_KEY"}),
    )
    with pytest.raises(RuntimeError, match="multiple owners"):
        resolve_owner_upstream_keys()


def test_missing_env_var_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_OWNER_KEY_ENVS", json.dumps({"org-a": "MISSING_OWNER_KEY"}))
    monkeypatch.delenv("MISSING_OWNER_KEY", raising=False)
    with pytest.raises(RuntimeError, match="missing or empty"):
        resolve_owner_upstream_keys()


def test_hyphenated_without_mapping_does_not_use_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove ARB34-001 canonicalize: org-a must not resolve PROXY_OWNER_ORG_A_*."""
    monkeypatch.delenv("PROXY_OWNER_KEY_ENVS", raising=False)
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16chars")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-default-env")
    monkeypatch.setenv("PROXY_OWNER_ORG_A_OPENAI_API_KEY", "sk-legacy-org-a")
    settings = load_settings()
    # Underscore / exact legacy fragment still works via derived upper path.
    assert settings.upstream_key_for_owner("org_a") == "sk-legacy-org-a"
    assert settings.upstream_key_for_owner("ORG_A") == "sk-legacy-org-a"
    # Hyphenated falls back to default (no silent remap to ORG_A).
    assert settings.upstream_key_for_owner("org-a") == "sk-default-env"


def test_hyphenated_without_mapping_or_default_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY_OWNER_KEY_ENVS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16chars")
    monkeypatch.setenv("PROXY_OWNER_ORG_A_OPENAI_API_KEY", "sk-legacy")
    settings = load_settings()
    with pytest.raises(KeyError, match="hyphenated owner"):
        settings.upstream_key_for_owner("org-a")


def test_case_collision_same_env_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACME_KEY", "sk-acme")
    monkeypatch.setenv(
        "PROXY_OWNER_KEY_ENVS",
        json.dumps({"Acme": "ACME_KEY", "acme": "ACME_KEY"}),
    )
    with pytest.raises(RuntimeError, match="multiple owners"):
        resolve_owner_upstream_keys()


def test_case_variants_with_distinct_envs_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACME_UPPER_KEY", "sk-upper")
    monkeypatch.setenv("ACME_LOWER_KEY", "sk-lower")
    monkeypatch.setenv(
        "PROXY_OWNER_KEY_ENVS",
        json.dumps({"Acme": "ACME_UPPER_KEY", "acme": "ACME_LOWER_KEY"}),
    )
    resolved = resolve_owner_upstream_keys()
    assert resolved["Acme"] == "sk-upper"
    assert resolved["acme"] == "sk-lower"
    settings = _base_settings(owner_upstream_keys=resolved)
    assert settings.upstream_key_for_owner("Acme") == "sk-upper"
    assert settings.upstream_key_for_owner("acme") == "sk-lower"


def test_validate_settings_loads_explicit_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16chars")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-default")
    monkeypatch.setenv("ORG_A_HYPHEN_KEY", "sk-hyphen")
    monkeypatch.setenv("PROXY_OWNER_KEY_ENVS", json.dumps({"org-a": "ORG_A_HYPHEN_KEY"}))
    cfg = validate_settings()
    assert cfg.upstream_key_for_owner("org-a") == "sk-hyphen"


def test_load_owner_key_env_map_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY_OWNER_KEY_ENVS", raising=False)
    assert load_owner_key_env_map() == {}
