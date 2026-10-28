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
