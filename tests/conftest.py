"""Pytest configuration and fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def clear_github_caches():
    """Clear module-level GitHub caches before each test."""
    from app.adapters.github import _clear_caches

    _clear_caches()
    yield
    _clear_caches()
