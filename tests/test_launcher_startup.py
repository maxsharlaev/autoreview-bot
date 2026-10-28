"""Launcher refuses to start without LAUNCHER_TOKEN."""

from __future__ import annotations

import pytest
from app.launcher.token import launcher_token_is_configured


def test_launcher_token_is_configured() -> None:
    assert launcher_token_is_configured("test-launcher-token-32chars-ok")
    assert not launcher_token_is_configured("")
    assert not launcher_token_is_configured("short")


@pytest.mark.asyncio
async def test_lifespan_refuses_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAUNCHER_TOKEN", raising=False)
    from app.launcher.main import lifespan
    from fastapi import FastAPI

    app = FastAPI()
    with pytest.raises(RuntimeError, match="LAUNCHER_TOKEN"):
        async with lifespan(app):
            pass


def test_require_volume_subpath_support_api_version() -> None:
    import httpx
    from app.launcher.docker_api import DockerApiError, DockerClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/version") or request.url.path == "/version":
            return httpx.Response(200, json={"ApiVersion": "1.44", "Version": "25.0.0"})
        if request.url.path.endswith("/_ping") or request.url.path == "/_ping":
            return httpx.Response(200, text="OK")
        return httpx.Response(404)

    client = DockerClient(client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://docker"))
    with pytest.raises(DockerApiError, match="1.45"):
        client.require_volume_subpath_support()


def test_require_volume_subpath_support_ok() -> None:
    import httpx
    from app.launcher.docker_api import DockerClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ApiVersion": "1.56", "Version": "29.8.0"})

    client = DockerClient(client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://docker"))
    client.require_volume_subpath_support()
