"""ARB34-004: finish-path proxy revoke is bounded so lease cleanup is not delayed."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from app.services.constants import CODEX_SANDBOX
from app.services.container_executor import (
    ContainerReviewExecutor,
    finish_revoke_timeout_seconds,
)


@pytest.mark.asyncio
async def test_slow_finish_revoke_does_not_delay_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAUNCHER_TOKEN", "test-launcher-token-32chars-ok")
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16ok!!")
    monkeypatch.setenv("MODEL_PROXY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path / "checkouts"))
    monkeypatch.setenv("OPR_PROXY_FINISH_REVOKE_TIMEOUT_SECONDS", "0.3")

    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / "README").write_text("x", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    output = tmp_path / "out.json"

    revoke_started = asyncio.Event()
    revoke_released = asyncio.Event()
    calls: list[str] = []

    async def fake_issue(**_kwargs: Any) -> str:
        return "opr_job_test_token_ok"

    async def hanging_revoke(*, job_id: str, **_kwargs: Any) -> bool:
        calls.append(job_id)
        revoke_started.set()
        await revoke_released.wait()
        return True

    async def fake_launcher(self, **_kwargs: Any) -> str:  # noqa: ANN001
        return '{"findings":[]}'

    monkeypatch.setattr(
        "app.services.container_executor.issue_job_token",
        fake_issue,
    )
    monkeypatch.setattr(
        "app.services.container_executor.revoke_job_token",
        hanging_revoke,
    )
    monkeypatch.setattr(
        ContainerReviewExecutor,
        "_call_launcher",
        fake_launcher,
    )
    # Avoid real FS heavy staging where possible — still runs stage; keep tiny.
    monkeypatch.setattr(
        "app.services.container_executor.safe_copytree",
        lambda src, dst, **kw: dst.mkdir(parents=True, exist_ok=True),
    )

    ex = ContainerReviewExecutor(launcher_url="http://127.0.0.1:1")
    budget = finish_revoke_timeout_seconds()
    assert budget == pytest.approx(0.3)

    started = time.monotonic()
    text = await ex.run(
        checkout=checkout,
        prompt="p",
        schema_path=schema,
        output_path=output,
        model="gpt-test",
        timeout_seconds=30,
        openai_api_key="sk-unused",
        owner_id="acme",
    )
    elapsed = time.monotonic() - started

    assert text == '{"findings":[]}'
    assert calls, "revoke should have been attempted"
    # Must return soon after revoke budget; not wait on hanging revoke.
    assert elapsed < budget + 1.5
    revoke_released.set()
    # Let the cancelled revoke task settle.
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_finish_revoke_timeout_still_cleans_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAUNCHER_TOKEN", "test-launcher-token-32chars-ok")
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16ok!!")
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path / "checkouts"))
    monkeypatch.setenv("OPR_PROXY_FINISH_REVOKE_TIMEOUT_SECONDS", "0.2")

    checkout = tmp_path / "repo"
    checkout.mkdir()
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    output = tmp_path / "out.json"

    async def fake_issue(**_kwargs: Any) -> str:
        return "opr_job_test_token_ok"

    async def hanging_revoke(**_kwargs: Any) -> bool:
        await asyncio.sleep(30)
        return True

    async def boom_launcher(self, **_kwargs: Any) -> str:  # noqa: ANN001
        from app.services.codex_runner import CodexRunnerError

        raise CodexRunnerError(CODEX_SANDBOX, "launcher failed")

    monkeypatch.setattr("app.services.container_executor.issue_job_token", fake_issue)
    monkeypatch.setattr("app.services.container_executor.revoke_job_token", hanging_revoke)
    monkeypatch.setattr(ContainerReviewExecutor, "_call_launcher", boom_launcher)
    monkeypatch.setattr(
        "app.services.container_executor.safe_copytree",
        lambda src, dst, **kw: dst.mkdir(parents=True, exist_ok=True),
    )

    ex = ContainerReviewExecutor(launcher_url="http://127.0.0.1:1")
    started = time.monotonic()
    from app.services.codex_runner import CodexRunnerError

    with pytest.raises(CodexRunnerError):
        await ex.run(
            checkout=checkout,
            prompt="p",
            schema_path=schema,
            output_path=output,
            model="gpt-test",
            timeout_seconds=30,
            openai_api_key="sk-unused",
        )
    elapsed = time.monotonic() - started
    assert elapsed < finish_revoke_timeout_seconds() + 1.5
