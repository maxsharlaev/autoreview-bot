"""Provider abstraction for upstream model APIs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.model_proxy.settings import ProxySettings


@dataclass(frozen=True)
class UpstreamTarget:
    scheme: str
    host: str
    path: str

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}{self.path}"


class Provider(Protocol):
    name: str

    def responses_target(self) -> UpstreamTarget:
        """Fixed upstream URL for POST /v1/responses (no caller override)."""
        ...


def build_provider(settings: ProxySettings) -> Provider:
    from app.model_proxy.providers.openai_provider import OpenAIProvider

    if settings.provider == "openai":
        return OpenAIProvider(host=settings.upstream_host, scheme=settings.upstream_scheme)
    raise RuntimeError(f"unsupported provider {settings.provider!r}")
