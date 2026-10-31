"""ARB34-009/010/011: uniform remove-after-confirm across finish/cancel/reaper."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from app.launcher.timeouts import (
    WORKER_CANCEL_HTTP_READ_SECONDS,
    launcher_cancel_deadline_seconds,
)
from app.services.model_proxy_client import RevokeAttemptOutcome
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_revoke_scheduler():
    from app.launcher import revoke_scheduler as rs

    rs._reset_for_tests()
    yield
    rs._reset_for_tests()


def test_config_error_keeps_evidence_until_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401/403/missing issuer ? CONFIG_ERROR; delete only after window expiry."""
    from app.launcher import reaper as reaper_mod
    from app.launcher import revoke_scheduler as rs
    from app.launcher.revoke_scheduler import RevokeJobStatus

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("LAUNCHER_REVOKE_CONFIG_ERROR_BACKOFF_SECONDS", "5")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)
    monkeypatch.setattr(reaper_mod, "directory_is_lease_stale", lambda path, now=None: True)

    async def config_err(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.CONFIG_ERROR

    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", config_err)

    job = "a" * 32
    created = time.time() - 100
    keep_dir = tmp_path / job
    keep_dir.mkdir()
    (keep_dir / "job.json").write_text(f'{{"created_unix": {int(created)}}}', encoding="utf-8")
    os.utime(keep_dir, (created, created))
    containers = [
        {
            "Id": "cccc",
            "Created": created,
            "State": "exited",
            "Names": ["/opr-review-a"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": job,
                "opr.created_unix": str(int(created)),
            },
        }
    ]
    removed: list[str] = []

    class FakeDocker:
        def list_review_containers(self):
            return list(containers)

        def kill_container(self, cid):
            return None

        def remove_container(self, cid, force=False):
            removed.append(cid)
            containers[:] = [c for c in containers if c.get("Id") != cid]
            return None

        def close(self):
            return None

    reaper_mod.reap_once(client=FakeDocker())  # type: ignore[arg-type]
    time.sleep(0.15)
    assert "cccc" not in removed
    assert keep_dir.exists()
    st = rs.revoke_status(job, created_at=created)
    assert st in (RevokeJobStatus.CONFIG_ERROR, RevokeJobStatus.PENDING)

    # Short window ? delete after expiry without confirmation.
    monkeypatch.setenv("LAUNCHER_REVOKE_RETRY_WINDOW_SECONDS", "1")
    created_old = time.time() - 10
    containers.append(
        {
            "Id": "dddd",
            "Created": created_old,
            "State": "exited",
            "Names": ["/opr-review-d"],
            "Labels": {
                "opr.review": "1",
                "opr.job_id": "d" * 32,
                "opr.created_unix": str(int(created_old)),
            },
        }
    )
    # Reset so status re-evaluates window from created_at
    rs._reset_for_tests()
    assert rs.may_delete_after_revoke("d" * 32, created_at=created_old) is True
    removed.clear()
    reaper_mod.reap_once(client=FakeDocker())  # type: ignore[arg-type]
    assert "dddd" in removed


@pytest.mark.asyncio
async def test_finish_keeps_job_dir_when_revoke_not_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.container_executor import ContainerReviewExecutor

    monkeypatch.setenv("LAUNCHER_TOKEN", "test-launcher-token-32chars-ok")
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16ok!!")
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path / "checkouts"))
    monkeypatch.setenv("OPR_PROXY_FINISH_REVOKE_TIMEOUT_SECONDS", "0.5")

    checkout = tmp_path / "repo"
    checkout.mkdir()
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    output = tmp_path / "out.json"
    kept: list[Path] = []

    async def fake_issue(**_kwargs: Any) -> str:
        return "opr_job_test_token_ok"

    async def fail_revoke(**_kwargs: Any) -> RevokeAttemptOutcome:
        return RevokeAttemptOutcome.TRANSIENT

    async def ok_launcher(self, **_kwargs: Any) -> str:  # noqa: ANN001
        return '{"ok": true}'

    monkeypatch.setattr("app.services.container_executor.issue_job_token", fake_issue)
    monkeypatch.setattr("app.services.container_executor.revoke_job_token_attempt", fail_revoke)
    monkeypatch.setattr(ContainerReviewExecutor, "_call_launcher", ok_launcher)
    monkeypatch.setattr(
        "app.services.container_executor.safe_copytree",
        lambda src, dst, **kw: dst.mkdir(parents=True, exist_ok=True),
    )

    from app.services import container_executor as ce

    def track_remove(job_dir: Path) -> None:
        kept.append(job_dir)
        # Do not actually need to call real ? assert it is NOT called
        raise AssertionError("job_dir must not be removed when revoke not confirmed")

    monkeypatch.setattr(ce, "_remove_job_dir", track_remove)

    ex = ContainerReviewExecutor(launcher_url="http://127.0.0.1:1")
    text = await ex.run(
        checkout=checkout,
        prompt="p",
        schema_path=schema,
        output_path=output,
        model="gpt-test",
        timeout_seconds=30,
        openai_api_key="sk-unused",
    )
    assert "ok" in text
    # Staging dir should still exist under checkouts
    base = tmp_path / "checkouts"
    dirs = [p for p in base.iterdir() if p.is_dir()]
    assert dirs, "job_dir must be retained for reaper"


@pytest.mark.asyncio
async def test_finish_keeps_job_dir_on_revoke_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.container_executor import ContainerReviewExecutor

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

    async def hang_revoke(**_kwargs: Any) -> RevokeAttemptOutcome:
        await asyncio.sleep(30)
        return RevokeAttemptOutcome.DONE

    async def ok_launcher(self, **_kwargs: Any) -> str:  # noqa: ANN001
        return '{"ok": true}'

    monkeypatch.setattr("app.services.container_executor.issue_job_token", fake_issue)
    monkeypatch.setattr("app.services.container_executor.revoke_job_token_attempt", hang_revoke)
    monkeypatch.setattr(ContainerReviewExecutor, "_call_launcher", ok_launcher)
    monkeypatch.setattr(
        "app.services.container_executor.safe_copytree",
        lambda src, dst, **kw: dst.mkdir(parents=True, exist_ok=True),
    )

    ex = ContainerReviewExecutor(launcher_url="http://127.0.0.1:1")
    await ex.run(
        checkout=checkout,
        prompt="p",
        schema_path=schema,
        output_path=output,
        model="gpt-test",
        timeout_seconds=30,
        openai_api_key="sk-unused",
    )
    base = tmp_path / "checkouts"
    assert any(p.is_dir() for p in base.iterdir())


def test_run_review_job_finally_stops_not_removes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """finish path in launcher: kill only, never remove_container."""
    from app.launcher import jobs as jobs_mod

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_CHECKOUT_ORPHAN_TTL_SECONDS", "900")
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "600")
    from app.launcher.job_lifecycle import JobLifecycleRegistry

    jobs_mod.lifecycle = JobLifecycleRegistry()
    job_id = "f" * 32
    job_dir = tmp_path / job_id
    repo = job_dir / "repo"
    repo.mkdir(parents=True)
    (job_dir / ".opr-work").mkdir(parents=True, exist_ok=True)
    (job_dir / "prompt.txt").write_text("p", encoding="utf-8")
    (job_dir / "schema.json").write_text("{}", encoding="utf-8")
    (job_dir / "job.json").write_text(
        '{"model":"m","timeout_seconds":30,"created_unix":1}',
        encoding="utf-8",
    )
    (job_dir / "output.json").write_text('{"ok":1}', encoding="utf-8")

    killed: list[str] = []
    removed: list[str] = []

    class FakeDocker:
        def create_container(self, name, config):
            return "cid1234567890ab"

        def start_container(self, cid):
            return None

        def wait_container(self, cid, timeout_seconds=None):
            work_out = tmp_path / job_id / ".opr-work"
            work_out.mkdir(parents=True, exist_ok=True)
            (work_out / "out.json").write_text('{"ok":1}', encoding="utf-8")
            return 0

        def logs(self, cid):
            return ""

        def kill_container(self, cid, timeout=None):
            killed.append(cid)

        def remove_container(self, cid, force=True, timeout=None):
            removed.append(cid)

        def close(self):
            return None

    monkeypatch.setattr(jobs_mod, "assert_safe_host_config", lambda hc: None)
    monkeypatch.setattr(
        jobs_mod,
        "build_host_config",
        lambda **kw: {"Binds": [], "NetworkMode": "bridge"},
    )
    monkeypatch.setattr(
        "app.launcher.codex_config.render_opr_proxy_config",
        lambda: "#test",
    )

    result = jobs_mod.run_review_job(
        job_id=job_id,
        checkout_ref=f"{job_id}/repo",
        proxy_token="opr_job_test_token_ok_long",
        client=FakeDocker(),  # type: ignore[arg-type]
    )
    assert result["status"] == "ok"
    assert killed == ["cid1234567890ab"]
    assert removed == []


@pytest.fixture()
def cancel_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    token = "test-launcher-token-32chars-ok"
    monkeypatch.setenv("LAUNCHER_TOKEN", token)
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("LAUNCHER_CANCEL_DEADLINE_SECONDS", "0.8")
    monkeypatch.setenv("LAUNCHER_CANCEL_WAIT_SECONDS", "0.3")
    monkeypatch.setenv("LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS", "5.0")
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16ok!!")
    monkeypatch.setenv("MODEL_PROXY_URL", "http://127.0.0.1:9")

    from app.launcher.auth import require_launcher_auth
    from app.launcher.main import cancel_job

    app = FastAPI()
    app.add_api_route(
        "/v1/jobs/{job_id}",
        cancel_job,
        methods=["DELETE"],
        dependencies=[Depends(require_launcher_auth)],
    )
    return TestClient(app), token


def test_cancel_deadline_still_met(cancel_app, monkeypatch: pytest.MonkeyPatch) -> None:
    client, token = cancel_app
    release = threading.Event()

    async def hang(*, job_id: str, **_kwargs: Any) -> RevokeAttemptOutcome:
        await asyncio.get_event_loop().run_in_executor(None, release.wait)
        return RevokeAttemptOutcome.DONE

    def fast_cancel(*, job_id: str, client: Any | None = None) -> dict:
        return {"job_id": job_id, "removed": 1, "confirmed": True}

    monkeypatch.setattr("app.launcher.jobs.cancel_review_job", fast_cancel)
    monkeypatch.setattr("app.services.model_proxy_client.revoke_job_token_attempt", hang)
    t0 = time.monotonic()
    r = client.delete(f"/v1/jobs/{'e' * 32}", headers={"Authorization": f"Bearer {token}"})
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert elapsed < launcher_cancel_deadline_seconds() + 0.75
    assert elapsed < WORKER_CANCEL_HTTP_READ_SECONDS
    release.set()


def test_attempt_classification_unit() -> None:
    """DONE only for 2xx/404; CONFIG_ERROR for auth/other 4xx/missing issuer."""
    import asyncio

    from app.services import model_proxy_client as mpc

    async def run():
        # missing issuer
        old = mpc.proxy_issuer_token
        mpc.proxy_issuer_token = lambda: ""  # type: ignore[assignment]
        try:
            out = await mpc.revoke_job_token_attempt(job_id="a" * 32)
            assert out is RevokeAttemptOutcome.CONFIG_ERROR
        finally:
            mpc.proxy_issuer_token = old  # type: ignore[assignment]

    asyncio.run(run())
