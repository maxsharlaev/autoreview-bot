"""Tests for GitHub module-level caches: TTL, eviction, and retry behavior."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from app.adapters.github import (
    _INSTALLATION_ID_CACHE,
    _INSTALLATION_ID_CACHE_TTL,
    _INSTALLATION_TOKEN_CACHE,
    GitHubAppClient,
    GitHubError,
    _clear_caches,
    _evict_caches_for_installation,
)
from app.config import Settings


def _make_client(app_id: int = 12345, installation_id: int = 0) -> GitHubAppClient:
    """Create a GitHubAppClient with mock settings."""
    settings = Settings.model_construct(
        github_app_id=app_id,
        github_app_private_key="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
        github_installation_id=installation_id,
        github_token="",
    )
    client = GitHubAppClient(settings=settings)
    return client


class TestInstallationIdCache:
    """Tests for installation ID cache with TTL."""

    @pytest.mark.asyncio
    async def test_installation_id_cached_with_ttl(self):
        """Installation ID should be cached with TTL."""
        client = _make_client()
        call_count = 0

        async def mock_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            mock.json.return_value = {"id": 67890}
            return mock

        with (
            patch.object(client, "_jwt", return_value="mock_jwt"),
            patch.object(client, "_request", side_effect=mock_request),
        ):
            # First call fetches from API
            result1 = await client.resolve_installation_id("org", "repo")
            assert result1 == 67890
            assert call_count == 1

            # Second call uses cache
            client._installation_id = 0  # Reset instance cache
            result2 = await client.resolve_installation_id("org", "repo")
            assert result2 == 67890
            assert call_count == 1  # No new API call

    @pytest.mark.asyncio
    async def test_installation_id_cache_expires(self):
        """Installation ID cache should expire after TTL."""
        client = _make_client()
        call_count = 0
        return_id = 67890

        async def mock_request(method, url, **kwargs):
            nonlocal call_count, return_id
            call_count += 1
            mock = MagicMock()
            mock.json.return_value = {"id": return_id}
            return mock

        with (
            patch.object(client, "_jwt", return_value="mock_jwt"),
            patch.object(client, "_request", side_effect=mock_request),
        ):
            # First call
            await client.resolve_installation_id("org", "repo")

            # Manually expire the cache
            cache_key = (12345, "org/repo")
            old_id, _ = _INSTALLATION_ID_CACHE[cache_key]
            _INSTALLATION_ID_CACHE[cache_key] = (old_id, time.time() - _INSTALLATION_ID_CACHE_TTL - 1)
            client._installation_id = 0

            # Second call should fetch again
            return_id = 11111
            result = await client.resolve_installation_id("org", "repo")
            assert result == 11111
            assert call_count == 2


class TestInstallationTokenCache:
    """Tests for installation token cache."""

    @pytest.mark.asyncio
    async def test_token_cached(self):
        """Installation token should be cached."""
        client = _make_client(installation_id=67890)
        call_count = 0

        async def mock_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            mock.json.return_value = {"token": "ghs_test123"}
            return mock

        with (
            patch.object(client, "_jwt", return_value="mock_jwt"),
            patch.object(client, "_request", side_effect=mock_request),
        ):
            # First call fetches from API
            token1 = await client.installation_token("org", "repo")
            assert token1 == "ghs_test123"
            assert call_count == 1

            # Second call uses cache (need new client instance to avoid instance cache)
            client2 = _make_client(installation_id=67890)
            with (
                patch.object(client2, "_jwt", return_value="mock_jwt"),
                patch.object(client2, "_request", side_effect=mock_request),
            ):
                token2 = await client2.installation_token("org", "repo")
                assert token2 == "ghs_test123"
                # No new API call due to module-level cache
                assert call_count == 1


class TestCacheEviction:
    """Tests for cache eviction on 401/404."""

    @pytest.mark.asyncio
    async def test_401_during_token_fetch_raises_error(self):
        """401 during token fetch should raise error (no retry per spec)."""
        client = _make_client(installation_id=67890)

        async def mock_request(method, url, **kwargs):
            raise GitHubError("401 Unauthorized", status_code=401)

        with (
            patch.object(client, "_jwt", return_value="mock_jwt"),
            patch.object(client, "_request", side_effect=mock_request),
        ):
            with pytest.raises(GitHubError, match="401"):
                await client.installation_token("org", "repo")

    @pytest.mark.asyncio
    async def test_404_during_installation_id_lookup_raises_error(self):
        """404 during installation ID lookup should raise error (no retry per spec)."""
        client = _make_client()

        async def mock_request(method, url, **kwargs):
            raise GitHubError("404 Not Found", status_code=404)

        with (
            patch.object(client, "_jwt", return_value="mock_jwt"),
            patch.object(client, "_request", side_effect=mock_request),
        ):
            with pytest.raises(GitHubError, match="404"):
                await client.resolve_installation_id("org", "repo")

    @pytest.mark.asyncio
    async def test_static_installation_id_skips_lookup(self):
        """When installation_id is statically configured, skip lookup entirely."""
        client = _make_client(installation_id=67890)

        # No mock_request needed - should not make any API call
        result = await client.resolve_installation_id("org", "repo")
        assert result == 67890


class TestEvictCachesForInstallation:
    """Tests for _evict_caches_for_installation helper."""

    def test_evicts_token_cache(self):
        """Should evict token cache entry for given installation."""
        _INSTALLATION_TOKEN_CACHE[(12345, 67890)] = ("token", time.time())
        _INSTALLATION_TOKEN_CACHE[(12345, 11111)] = ("other", time.time())

        _evict_caches_for_installation(12345, 67890)

        assert (12345, 67890) not in _INSTALLATION_TOKEN_CACHE
        assert (12345, 11111) in _INSTALLATION_TOKEN_CACHE

    def test_evicts_installation_id_cache(self):
        """Should evict installation ID cache entries mapping to given installation."""
        _INSTALLATION_ID_CACHE[(12345, "org/repo")] = (67890, time.time())
        _INSTALLATION_ID_CACHE[(12345, "org/other")] = (11111, time.time())
        _INSTALLATION_ID_CACHE[(99999, "org/repo")] = (67890, time.time())

        _evict_caches_for_installation(12345, 67890)

        assert (12345, "org/repo") not in _INSTALLATION_ID_CACHE
        assert (12345, "org/other") in _INSTALLATION_ID_CACHE
        assert (99999, "org/repo") in _INSTALLATION_ID_CACHE  # Different app_id


class TestClearCaches:
    """Tests for _clear_caches function."""

    def test_clears_all_caches(self):
        """_clear_caches should clear all module-level caches."""
        _INSTALLATION_TOKEN_CACHE[(1, 2)] = ("token", 0)
        _INSTALLATION_ID_CACHE[(1, "x")] = (2, 0)

        _clear_caches()

        assert len(_INSTALLATION_TOKEN_CACHE) == 0
        assert len(_INSTALLATION_ID_CACHE) == 0
