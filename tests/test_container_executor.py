"""ContainerReviewExecutor: launcher client + fail-closed behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from app.services.codex_runner import CodexRunnerError
from app.services.constants import CODEX_SANDBOX, CODEX_TIMEOUT
from app.services.container_executor import ContainerReviewExecutor
from app.services.review_executor import CONTAINER_BACKEND, get_review_executor

PROXY_TOKEN = "opr_job_test_proxy_token_value_ok"


def _stub_proxy_issuer(monkeypatch: pytest.MonkeyPatch, *, token: str = PROXY_TOKEN) -> list[str]:
    """Stub model-proxy issue/revoke; return list of revoked job ids."""
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "test-issuer-token-32chars-ok!!")
    monkeypatch.setenv("MODEL_PROXY_URL", "http://model-proxy:8081")
    revoked: list[str] = []

    async def _issue(*, job_id: str, timeout_seconds: int, owner_id: str = "", **kwargs):
        assert job_id
        assert timeout_seconds > 0
        return token

    async def _revoke(*, job_id: str, **kwargs):
        from app.services.model_proxy_client import RevokeAttemptOutcome

        revoked.append(job_id)
        return RevokeAttemptOutcome.DONE

    monkeypatch.setattr("app.services.container_executor.issue_job_token", _issue)
    monkeypatch.setattr("app.services.container_executor.revoke_job_token_attempt", _revoke)
    return revoked


@pytest.mark.asyncio
async def test_container_executor_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path / "base"))
    _stub_proxy_issuer(monkeypatch)
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "file.py").write_text("x=1\n", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    out = tmp_path / "out.json"

    revoked = _stub_proxy_issuer(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-launcher-token-32chars-ok"
        assert "X-Model-Api-Key" not in request.headers
        body = json_body(request)
        assert set(body) == {"job_id", "checkout_ref", "proxy_token"}
        assert body["checkout_ref"].endswith("/repo")
        assert body["proxy_token"] == PROXY_TOKEN
        assert not str(body["proxy_token"]).startswith("sk-")
        return httpx.Response(
            200, json={"job_id": body["job_id"], "status": "ok", "output": '{"ok":true}', "duration_seconds": 0.1}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://launcher") as client:
        ex = ContainerReviewExecutor(
            launcher_url="http://launcher",
            launcher_token="test-launcher-token-32chars-ok",
            checkout_base=tmp_path / "base",
            http_client=client,
        )
        text = await ex.run(
            checkout=checkout,
            prompt="hello",
            schema_path=schema,
            output_path=out,
            model="gpt-test",
            timeout_seconds=30,
            openai_api_key="sk-real-must-not-be-sent",
            owner_id="acme",
        )
    assert text == '{"ok":true}'
    assert out.read_text(encoding="utf-8") == '{"ok":true}'
    assert len(revoked) == 1


@pytest.mark.asyncio
async def test_container_executor_fail_closed_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_proxy_issuer(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": {"code": "CODEX_SANDBOX", "message": "boom"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://launcher") as client:
        ex = ContainerReviewExecutor(
            launcher_url="http://launcher",
            launcher_token="test-launcher-token-32chars-ok",
            checkout_base=tmp_path / "base",
            http_client=client,
        )
        with pytest.raises(CodexRunnerError) as excinfo:
            await ex.run(
                checkout=tmp_path,
                prompt="p",
                schema_path=_touch(tmp_path / "s.json", "{}"),
                output_path=tmp_path / "o.json",
                model="m",
                timeout_seconds=10,
                openai_api_key="sk",
            )
    assert excinfo.value.code == CODEX_SANDBOX


@pytest.mark.asyncio
async def test_container_executor_requires_token(tmp_path: Path) -> None:
    ex = ContainerReviewExecutor(launcher_token="", checkout_base=tmp_path)
    with pytest.raises(CodexRunnerError) as excinfo:
        await ex.run(
            checkout=tmp_path,
            prompt="p",
            schema_path=_touch(tmp_path / "s.json", "{}"),
            output_path=tmp_path / "o.json",
            model="m",
            timeout_seconds=10,
            openai_api_key="sk",
        )
    assert excinfo.value.code == CODEX_SANDBOX


def test_factory_returns_container_executor() -> None:
    assert type(get_review_executor(CONTAINER_BACKEND)).__name__ == "ContainerReviewExecutor"


def json_body(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content.decode("utf-8"))


def _touch(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_container_executor_http_timeout_derived(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no http_client is injected, read timeout follows timeout_seconds + overhead."""
    _stub_proxy_issuer(monkeypatch)
    from app.launcher.timeouts import launcher_http_timeout_seconds

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path / "base"))
    _stub_proxy_issuer(monkeypatch)
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {"job_id": "x", "status": "ok", "output": "{}", "duration_seconds": 0.0}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            captured["timeout"] = kwargs.get("timeout")

        async def post(self, *args, **kwargs):
            return FakeResponse()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("app.services.container_executor.httpx.AsyncClient", FakeClient)
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "f.py").write_text("x=1\n", encoding="utf-8")
    ex = ContainerReviewExecutor(
        launcher_url="http://launcher",
        launcher_token="test-launcher-token-32chars-ok",
        checkout_base=tmp_path / "base",
    )
    await ex.run(
        checkout=checkout,
        prompt="p",
        schema_path=_touch(tmp_path / "s.json", "{}"),
        output_path=tmp_path / "o.json",
        model="m",
        timeout_seconds=120,
        openai_api_key="sk",
    )
    timeout = captured["timeout"]
    assert timeout is not None
    assert timeout.read == launcher_http_timeout_seconds(120)


@pytest.mark.asyncio
async def test_container_executor_cancel_deletes_job_and_removes_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path / "base"))
    _stub_proxy_issuer(monkeypatch)
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "file.py").write_text("x=1\n", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    out = tmp_path / "out.json"
    seen_delete: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            seen_delete.append(request.url.path)
            return httpx.Response(200, json={"job_id": "x", "removed": 1})
        # Hang simulation: raise CancelledError via side channel — use slow path
        raise httpx.ReadTimeout("slow")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://launcher") as client:
        ex = ContainerReviewExecutor(
            launcher_url="http://launcher",
            launcher_token="test-launcher-token-32chars-ok",
            checkout_base=tmp_path / "base",
            http_client=client,
        )

        async def boom(*args, **kwargs):  # noqa: ANN001
            raise asyncio.CancelledError()

        monkeypatch.setattr(ex, "_call_launcher", boom)
        cancels: list[str] = []

        async def fake_cancel(job_id: str) -> bool:
            cancels.append(job_id)
            return True

        monkeypatch.setattr(ex, "_cancel_job", fake_cancel)
        with pytest.raises(asyncio.CancelledError):
            await ex.run(
                checkout=checkout,
                prompt="hello",
                schema_path=schema,
                output_path=out,
                model="gpt-test",
                timeout_seconds=30,
                openai_api_key="sk-test",
            )
    assert cancels
    # job dir removed after confirmed cancel
    base = tmp_path / "base"
    leftovers = [p for p in base.iterdir()] if base.exists() else []
    assert leftovers == []


@pytest.mark.asyncio
async def test_container_executor_leaves_job_dir_when_cancel_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path / "base"))
    _stub_proxy_issuer(monkeypatch)
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "file.py").write_text("x=1\n", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    out = tmp_path / "out.json"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        base_url="http://launcher",
    ) as client:
        ex = ContainerReviewExecutor(
            launcher_url="http://launcher",
            launcher_token="test-launcher-token-32chars-ok",
            checkout_base=tmp_path / "base",
            http_client=client,
        )

        async def boom(*args, **kwargs):  # noqa: ANN001
            raise asyncio.CancelledError()

        monkeypatch.setattr(ex, "_call_launcher", boom)

        async def fake_cancel(job_id: str) -> bool:
            return False

        monkeypatch.setattr(ex, "_cancel_job", fake_cancel)
        with pytest.raises(asyncio.CancelledError):
            await ex.run(
                checkout=checkout,
                prompt="hello",
                schema_path=schema,
                output_path=out,
                model="gpt-test",
                timeout_seconds=30,
                openai_api_key="sk-test",
            )
    base = tmp_path / "base"
    leftovers = sorted(base.iterdir(), key=lambda p: p.name)
    dirs = [p for p in leftovers if p.is_dir()]
    leases = [p for p in leftovers if p.suffix == ".lease"]
    assert len(dirs) == 1
    assert len(leases) == 1
    assert leases[0].name == f"{dirs[0].name}.lease"


@pytest.mark.asyncio
async def test_container_executor_timeout_triggers_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_proxy_issuer(monkeypatch)
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path / "base"))
    _stub_proxy_issuer(monkeypatch)
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "file.py").write_text("x=1\n", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    out = tmp_path / "out.json"
    cancels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            cancels.append(request.url.path)
            return httpx.Response(200, json={"job_id": "x", "removed": 1})
        raise httpx.ReadTimeout("timed out")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://launcher") as client:
        ex = ContainerReviewExecutor(
            launcher_url="http://launcher",
            launcher_token="test-launcher-token-32chars-ok",
            checkout_base=tmp_path / "base",
            http_client=client,
        )
        with pytest.raises(CodexRunnerError) as ei:
            await ex.run(
                checkout=checkout,
                prompt="hello",
                schema_path=schema,
                output_path=out,
                model="gpt-test",
                timeout_seconds=30,
                openai_api_key="sk-test",
            )
    assert ei.value.code == CODEX_TIMEOUT
    assert cancels


@pytest.mark.asyncio
async def test_container_executor_propagates_codex_timeout_from_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARB-27-022: launcher detail code CODEX_TIMEOUT must surface (not remapped to SANDBOX)."""
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path / "base"))
    _stub_proxy_issuer(monkeypatch)
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "file.py").write_text("x=1\n", encoding="utf-8")
    schema = _touch(tmp_path / "s.json", "{}")
    out = tmp_path / "o.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200, json={"job_id": "x", "removed": 1, "confirmed": True})
        return httpx.Response(
            500,
            json={"detail": {"code": "CODEX_TIMEOUT", "message": "review container timed out"}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://launcher") as client:
        ex = ContainerReviewExecutor(
            launcher_url="http://launcher",
            launcher_token="test-launcher-token-32chars-ok",
            checkout_base=tmp_path / "base",
            http_client=client,
        )
        with pytest.raises(CodexRunnerError) as excinfo:
            await ex.run(
                checkout=checkout,
                prompt="p",
                schema_path=schema,
                output_path=out,
                model="m",
                timeout_seconds=10,
                openai_api_key="sk",
            )
    assert excinfo.value.code == CODEX_TIMEOUT
