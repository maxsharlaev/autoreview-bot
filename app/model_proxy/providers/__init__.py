"""Upstream model providers (OpenAI now; more later)."""

from __future__ import annotations

from app.model_proxy.providers.base import Provider, build_provider
from app.model_proxy.providers.openai_provider import OpenAIProvider

__all__ = ["Provider", "OpenAIProvider", "build_provider"]
