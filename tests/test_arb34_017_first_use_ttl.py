"""ARB34-017: token lifetime starts at first use; unused capped by first-use TTL."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.model_proxy.settings import ProxySettings, load_settings
from app.model_proxy.tokens import TokenStore


def _settings(**over) -> ProxySettings:
    base = dict(
        issuer_token="x" * 32,
        default_upstream_key="sk-test",
        owner_upstream_keys={},
        legacy_owner_upstream_keys={},
        upstream_host="api.openai.com",
        upstream_scheme="https",
        provider="openai",
        max_review_timeout_seconds=600,
        token_grace_seconds=30,
        token_first_use_ttl_seconds=120,
        max_body_bytes=4 * 1024 * 1024,
        max_requests_per_job=50,
        max_bytes_per_job=16 * 1024 * 1024,
        max_concurrent_per_job=2,
        stream_idle_timeout_seconds=300.0,
        upstream_timeout_seconds=660.0,
    )
    base.update(over)
    return ProxySettings(**base)


def test_first_use_ttl_default_is_120(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROXY_TOKEN_FIRST_USE_TTL_SECONDS", raising=False)
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16ok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = load_settings()
    assert cfg.token_first_use_ttl_seconds == 120


def test_unused_token_expires_at_first_use_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    store = TokenStore(_settings(token_first_use_ttl_seconds=120, token_grace_seconds=30))
    issued = store.issue(job_id="a" * 32, timeout_seconds=600)
    assert issued.expires_in == 120
    assert abs(issued.expires_at - 1_120.0) < 1e-6
    # Still unused just before first-use TTL.
    clock["now"] = 1_119.0
    # Do not resolve yet — resolving would mark first use and extend TTL.
    with store._lock:
        rec = store._by_hash.get(store.hash_token(issued.token))
        assert rec is not None
        assert rec.first_used_at is None
        assert rec.expires_at == pytest.approx(1_120.0)
    clock["now"] = 1_121.0
    assert store.resolve(issued.token) is None


def test_startup_delay_past_grace_does_not_cut_review_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue → wait past grace but within first-use TTL → first use gets full timeout+grace."""
    clock = {"now": 1_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    store = TokenStore(
        _settings(
            max_review_timeout_seconds=600,
            token_grace_seconds=30,
            token_first_use_ttl_seconds=120,
        )
    )
    issued = store.issue(job_id="c" * 32, timeout_seconds=600)
    # Simulate container startup delay > grace, < first-use TTL.
    clock["now"] = 1_000.0 + 60.0
    rec = store.resolve(issued.token)
    assert rec is not None
    assert rec.first_used_at == pytest.approx(1_060.0)
    assert rec.expires_at == pytest.approx(1_060.0 + 600 + 30)
    # Still valid near end of post-first-use window.
    clock["now"] = 1_060.0 + 600 + 29
    assert store.still_valid(rec) is True
    clock["now"] = 1_060.0 + 600 + 31
    assert store.still_valid(rec) is False


def test_concurrent_first_requests_fix_one_time(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 5_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    store = TokenStore(_settings())
    issued = store.issue(job_id="d" * 32, timeout_seconds=100)
    barrier = threading.Barrier(8)
    results: list[float | None] = []

    def worker() -> None:
        barrier.wait(timeout=5)
        rec = store.resolve(issued.token)
        results.append(None if rec is None else rec.first_used_at)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(worker) for _ in range(8)]
        for f in futs:
            f.result(timeout=5)
    assert all(r == 5_000.0 for r in results)
    rec = store.resolve(issued.token)
    assert rec is not None
    assert rec.expires_at == pytest.approx(5_000.0 + 600 + 30)


def test_expiry_after_timeout_plus_grace_from_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 2_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    store = TokenStore(
        _settings(max_review_timeout_seconds=100, token_grace_seconds=30, token_first_use_ttl_seconds=120)
    )
    issued = store.issue(job_id="e" * 32, timeout_seconds=100)
    clock["now"] = 2_010.0
    rec = store.resolve(issued.token)
    assert rec is not None
    assert rec.expires_at == pytest.approx(2_010.0 + 130)
    clock["now"] = 2_010.0 + 129
    assert store.resolve(issued.token) is not None
    clock["now"] = 2_010.0 + 131
    assert store.resolve(issued.token) is None
