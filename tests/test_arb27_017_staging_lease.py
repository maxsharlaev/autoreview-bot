"""ARB-27-017: staging FS work must not starve lease heartbeats."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import httpx
import pytest
from app.launcher.reaper import reap_checkout_orphans
from app.services.container_executor import ContainerReviewExecutor


@pytest.mark.asyncio
async def test_slow_staging_keeps_job_dir_under_concurrent_reaper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging slower than lease-stale threshold; heartbeat keeps refreshing so reaper keeps dir."""
    monkeypatch.setenv("OPR_LEASE_HEARTBEAT_SECONDS", "1")  # stale after 3s
    base = tmp_path / "base"
    base.mkdir()
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "file.py").write_text("x=1\n", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    out = tmp_path / "out.json"

    import app.services.container_executor as ce

    real_copy = ce.safe_copytree
    loop = asyncio.get_running_loop()
    started = loop.time()
    staging_seen: list[Path] = []

    def slow_copy(src: Path, dst: Path) -> None:
        staging_seen.append(dst)
        time.sleep(4.0)
        real_copy(src, dst)

    monkeypatch.setattr(ce, "safe_copytree", slow_copy)

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "job_id": body["job_id"],
                "status": "ok",
                "output": "{}",
                "duration_seconds": 0.01,
            },
        )

    reaped_while_staging = 0

    async def reaper_loop() -> None:
        nonlocal reaped_while_staging
        while True:
            n = await asyncio.to_thread(reap_checkout_orphans, base_dir=base, live_job_ids=set())
            reaped_while_staging += n
            await asyncio.sleep(0.4)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://launcher") as client:
        ex = ContainerReviewExecutor(
            launcher_url="http://launcher",
            launcher_token="test-launcher-token-32chars-ok",
            checkout_base=base,
            http_client=client,
        )
        reaper_task = asyncio.create_task(reaper_loop())
        try:
            text = await ex.run(
                checkout=checkout,
                prompt="hello",
                schema_path=schema,
                output_path=out,
                model="gpt-test",
                timeout_seconds=30,
                openai_api_key="sk-test",
            )
        finally:
            reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper_task

    elapsed = loop.time() - started
    assert elapsed >= 4.0, "staging must have taken the slow path"
    assert staging_seen, "slow safe_copytree must have run"
    assert text == "{}"
    assert reaped_while_staging == 0
