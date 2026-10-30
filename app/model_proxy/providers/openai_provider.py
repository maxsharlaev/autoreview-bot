"""OpenAI Responses API upstream (api.openai.com)."""

from __future__ import annotations

from app.model_proxy.providers.base import UpstreamTarget


class OpenAIProvider:
    name = "openai"

    def __init__(self, *, host: str = "api.openai.com", scheme: str = "https") -> None:
        self._host = host
        self._scheme = scheme

    def responses_target(self) -> UpstreamTarget:
        return UpstreamTarget(scheme=self._scheme, host=self._host, path="/v1/responses")
