"""ARB34-001: hyphenated owner ids resolve per-owner proxy keys via canonicalize_owner_id."""

from __future__ import annotations

import pytest
from app.model_proxy.settings import (
    ProxySettings,
    canonicalize_owner_id,
    load_owner_upstream_keys,
    load_settings,
)


def _base_settings(**overrides: object) -> ProxySettings:
    defaults: dict[str, object] = {
        "issuer_token": "x" * 32,
        "default_upstream_key": "sk-default",
        "owner_upstream_keys": {"ORG_A": "sk-owner-org-a"},
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("org-a", "ORG_A"),
        ("ORG-A", "ORG_A"),
        ("org_a", "ORG_A"),
        ("ORG_A", "ORG_A"),
        ("  org-a  ", "ORG_A"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_canonicalize_owner_id(raw: str, expected: str) -> None:
    assert canonicalize_owner_id(raw) == expected


def test_canonicalize_owner_id_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="invalid owner id"):
        canonicalize_owner_id("org@a")
    with pytest.raises(ValueError, match="invalid owner id"):
        canonicalize_owner_id("org.a")


def test_hyphenated_owner_resolves_owner_key_not_default() -> None:
    settings = _base_settings()
    for oid in ("org-a", "ORG-A", "org_a", "Org-A"):
        assert settings.upstream_key_for_owner(oid) == "sk-owner-org-a"


def test_hyphenated_owner_without_default_key() -> None:
    settings = _base_settings(default_upstream_key="")
    assert settings.upstream_key_for_owner("org-a") == "sk-owner-org-a"
    with pytest.raises(KeyError, match="no upstream model key"):
        settings.upstream_key_for_owner("other-owner")


def test_unknown_owner_falls_back_to_default() -> None:
    settings = _base_settings()
    assert settings.upstream_key_for_owner("other") == "sk-default"


def test_load_owner_upstream_keys_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_OWNER_ORG_A_OPENAI_API_KEY", "sk-from-env")
    monkeypatch.setenv("PROXY_OWNER_ACME_OPENAI_API_KEY", "sk-acme")
    # Empty value ignored
    monkeypatch.setenv("PROXY_OWNER_EMPTY_OPENAI_API_KEY", "  ")
    keys = load_owner_upstream_keys()
    assert keys == {"ORG_A": "sk-from-env", "ACME": "sk-acme"}


def test_load_settings_hyphen_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16chars")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-default-env")
    monkeypatch.setenv("PROXY_OWNER_ORG_A_OPENAI_API_KEY", "sk-owner-env")
    settings = load_settings()
    assert settings.upstream_key_for_owner("org-a") == "sk-owner-env"
    assert settings.upstream_key_for_owner("ORG-A") == "sk-owner-env"
    # Must not silently prefer default when owner key exists
    assert settings.upstream_key_for_owner("org-a") != settings.default_upstream_key
