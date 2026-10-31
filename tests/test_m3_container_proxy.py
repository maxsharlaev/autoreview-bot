"""M3 PR3: container path uses model-proxy job tokens (no real model key in launch spec)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from app.launcher.codex_config import render_opr_proxy_config
from app.launcher.spec import build_container_config, build_host_config

REAL_KEY = "sk-proj-REAL-SECRET-MUST-NEVER-APPEAR"
PROXY_TOKEN = "opr_job_deadbeefcafebabe0123456789abcdef"


def test_render_opr_proxy_config_has_provider_no_secrets() -> None:
    text = render_opr_proxy_config(base_url="http://model-proxy:8081/v1")
    assert 'model_provider = "opr_proxy"' in text
    assert "[model_providers.opr_proxy]" in text
    assert 'base_url = "http://model-proxy:8081/v1"' in text
    assert 'env_key = "CODEX_API_KEY"' in text
    assert 'wire_api = "responses"' in text
    assert "supports_websockets = false" in text
    assert REAL_KEY not in text
    assert "sk-" not in text
    assert PROXY_TOKEN not in text
    assert "opr_job_" not in text


def test_build_container_config_env_has_only_proxy_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.launcher import spec as spec_mod

    monkeypatch.setattr(spec_mod, "load_review_seccomp_profile", lambda path=None: '{"syscalls":[]}')
    monkeypatch.setattr(spec_mod, "review_apparmor_profile", lambda: "unconfined")
    host = build_host_config(
        volume_name="opr_checkouts",
        checkout_subpath="job1/repo",
        work_subpath="job1/.opr-work",
        prompt_subpath="job1/prompt.txt",
        schema_subpath="job1/schema.json",
    )
    cfg = build_container_config(
        job_id="a" * 32,
        host_config=host,
        model="gpt-test",
        timeout_seconds=30,
        reasoning_effort="low",
        sandbox="read-only",
        approval_policy="never",
        allow_unsandboxed=False,
        proxy_token=PROXY_TOKEN,
    )
    env = list(cfg["Env"])
    joined = "\n".join(env)
    assert f"CODEX_API_KEY={PROXY_TOKEN}" in env
    assert not any(e.startswith("OPENAI_API_KEY=") for e in env)
    assert REAL_KEY not in joined
    assert "sk-proj" not in joined
    assert "sk-" not in joined
    # Full launch spec sanity: no real key in labels/image either
    blob = json.dumps(cfg)
    assert REAL_KEY not in blob
    assert "sk-proj" not in blob


def test_run_review_job_writes_codex_config_and_uses_proxy_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.launcher import jobs as jobs_mod

    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path))
    monkeypatch.setenv("OPR_CHECKOUT_VOLUME", "opr_checkouts")
    monkeypatch.setenv("OPR_MODEL_PROXY_BASE_URL", "http://model-proxy:8081/v1")
    # Minimal seccomp/apparmor for assert_safe_host_config path via build
    from app.launcher import spec as spec_mod

    monkeypatch.setattr(spec_mod, "load_review_seccomp_profile", lambda path=None: '{"syscalls":[]}')
    monkeypatch.setattr(spec_mod, "review_apparmor_profile", lambda: "unconfined")

    job_id = "b" * 32
    job_dir = tmp_path / job_id
    repo = job_dir / "repo"
    repo.mkdir(parents=True)
    (job_dir / "prompt.txt").write_text("p", encoding="utf-8")
    (job_dir / "schema.json").write_text("{}", encoding="utf-8")
    (job_dir / "job.json").write_text(json.dumps({"model": "gpt-test", "timeout_seconds": 30}), encoding="utf-8")

    created: dict = {}

    class FakeDocker:
        def create_container(self, name, config):
            created["config"] = config
            work = tmp_path / job_id / ".opr-work"
            # jobs writes codex-config before create
            assert (work / "codex-config.toml").is_file()
            cfg_text = (work / "codex-config.toml").read_text(encoding="utf-8")
            assert "opr_proxy" in cfg_text
            assert REAL_KEY not in cfg_text
            assert PROXY_TOKEN not in cfg_text
            env = config["Env"]
            assert f"CODEX_API_KEY={PROXY_TOKEN}" in env
            assert REAL_KEY not in "\n".join(env)
            return "cid123"

        def start_container(self, cid):
            return None

        def wait_container(self, cid, timeout_seconds=0):
            # Produce output file
            out = tmp_path / job_id / ".opr-work" / "out.json"
            out.write_text('{"ok":true}', encoding="utf-8")
            return 0

        def logs(self, cid):
            return ""

        def remove_container(self, cid, force=False):
            return None

        def close(self):
            return None

    monkeypatch.setattr(jobs_mod.lifecycle, "accept", lambda jid: None)
    monkeypatch.setattr(jobs_mod.lifecycle, "check_not_cancelled", lambda jid: None)
    monkeypatch.setattr(jobs_mod.lifecycle, "mark_creating", lambda jid: None)
    monkeypatch.setattr(jobs_mod.lifecycle, "mark_running", lambda jid, cid: None)
    monkeypatch.setattr(jobs_mod.lifecycle, "mark_finished", lambda jid, cancelled=False: None)
    monkeypatch.setattr(jobs_mod.lifecycle, "is_cancelled", lambda jid: False)

    result = jobs_mod.run_review_job(
        job_id=job_id,
        checkout_ref=f"{job_id}/repo",
        proxy_token=PROXY_TOKEN,
        client=FakeDocker(),  # type: ignore[arg-type]
    )
    assert result["status"] == "ok"
    assert created["config"]


def test_launcher_rejects_legacy_model_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARB34-007: legacy X-Model-Api-Key yields 426; value never forwarded."""
    from fastapi import HTTPException

    def boom(**kwargs):
        raise AssertionError("run_review_job must not run when legacy header is present")

    monkeypatch.setattr("app.launcher.main.run_review_job", boom)
    from app.launcher.main import RunReviewRequest, start_review

    body = RunReviewRequest(
        job_id="c" * 32,
        checkout_ref=("c" * 32) + "/repo",
        proxy_token=PROXY_TOKEN,
    )
    with pytest.raises(HTTPException) as ei:
        start_review(body=body, x_model_api_key=REAL_KEY)
    assert ei.value.status_code == 426
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "CODEX_UPGRADE_REQUIRED"
    assert REAL_KEY not in str(detail)


@pytest.mark.asyncio
async def test_revoke_on_finish_and_cancel_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.services.container_executor import ContainerReviewExecutor

    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "test-issuer-token-32chars-ok!!")
    monkeypatch.setenv("MODEL_PROXY_URL", "http://model-proxy:8081")
    monkeypatch.setenv("OPR_CHECKOUT_BASE", str(tmp_path / "base"))

    events: list[str] = []

    async def _issue(*, job_id: str, timeout_seconds: int, owner_id: str = "", **kwargs):
        events.append(f"issue:{job_id}:{owner_id}")
        return PROXY_TOKEN

    async def _revoke(*, job_id: str, **kwargs):
        events.append(f"revoke:{job_id}")
        return True

    monkeypatch.setattr("app.services.container_executor.issue_job_token", _issue)
    monkeypatch.setattr("app.services.container_executor.revoke_job_token", _revoke)

    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "a.py").write_text("x=1\n", encoding="utf-8")
    schema = tmp_path / "s.json"
    schema.write_text("{}", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["proxy_token"] == PROXY_TOKEN
        assert REAL_KEY not in request.content.decode()
        assert "X-Model-Api-Key" not in request.headers
        return httpx.Response(
            200,
            json={"job_id": body["job_id"], "status": "ok", "output": '{"ok":true}', "duration_seconds": 0.1},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://launcher") as client:
        ex = ContainerReviewExecutor(
            launcher_url="http://launcher",
            launcher_token="test-launcher-token-32chars-ok",
            checkout_base=tmp_path / "base",
            http_client=client,
        )
        await ex.run(
            checkout=checkout,
            prompt="p",
            schema_path=schema,
            output_path=tmp_path / "out.json",
            model="m",
            timeout_seconds=30,
            openai_api_key=REAL_KEY,
            owner_id="owner-a",
        )
    assert any(e.startswith("issue:") and e.endswith(":owner-a") for e in events)
    assert any(e.startswith("revoke:") for e in events)


def test_reaper_revokes_on_container_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.launcher import reaper as reaper_mod

    revoked: list[str] = []

    def fake_schedule(job_id: str, *, created_at: float | None = None) -> bool:
        revoked.append(job_id)
        return True

    monkeypatch.setattr(reaper_mod, "schedule_proxy_token_revoke", fake_schedule)
    monkeypatch.setattr(
        reaper_mod,
        "may_delete_after_revoke",
        lambda jid, created_at=None, now=None: True,
    )
    monkeypatch.setattr(reaper_mod, "orphan_ttl_seconds", lambda: 1)
    monkeypatch.setattr(reaper_mod.lifecycle, "launch_active", lambda jid: False)

    class FakeDocker:
        def list_review_containers(self):
            return [
                {
                    "Id": "deadbeef",
                    "Created": 0,
                    "Names": ["/opr-review-x"],
                    "Labels": {"opr.review": "1", "opr.job_id": "d" * 32},
                }
            ]

        def kill_container(self, cid):
            return None

        def remove_container(self, cid, force=False):
            return None

        def close(self):
            return None

    monkeypatch.setattr(reaper_mod, "reap_checkout_orphans", lambda **kwargs: 0)
    removed = reaper_mod.reap_once(client=FakeDocker())  # type: ignore[arg-type]
    assert removed >= 1
    assert "d" * 32 in revoked
