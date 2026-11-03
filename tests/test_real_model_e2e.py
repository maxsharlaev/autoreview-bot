"""Opt-in real-model E2E: container backend + model-proxy happy path (M4 PR2).

The happy-path test is skipped unless ``OPR_REAL_MODEL_E2E=1`` (and Docker is
available). Never enabled in CI. Upstream key is read **only** from
``model-proxy.env`` (ambient ``OPENAI_API_KEY`` is ignored). The key value is
never printed on failure and is never placed on the docker argv.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

docker = shutil.which("docker")
enabled = os.environ.get("OPR_REAL_MODEL_E2E", "").strip().lower() in {"1", "true", "yes"}

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SCHEMA = ROOT / "schemas" / "review_output.json"
DEFAULT_MODEL = os.environ.get("OPR_REAL_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
HEAD_SHA = "deadbeefcafebabe0123456789abcdef01234567"
MODEL_PROXY_ENV_NAME = "model-proxy.env"


def _run(
    args: list[str],
    *,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=True, text=True, env=env)


def _rm_container_idempotent(name: str) -> None:
    """Best-effort `docker rm -f`; missing container is not an error (ARB37-003)."""
    if not docker or not name:
        return
    result = _run([docker, "rm", "-f", name])
    if result.returncode == 0:
        return
    err = f"{result.stderr or ''}{result.stdout or ''}".lower()
    if "no such container" in err:
        return
    # Still do not raise from cleanup paths.


def load_upstream_key_from_model_proxy_env(env_file: Path) -> str:
    """Return OPENAI_API_KEY from model-proxy.env only (never from os.environ)."""
    if not env_file.is_file():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        trim = line.strip()
        if not trim or trim.startswith("#") or "=" not in trim:
            continue
        name, _, val = trim.partition("=")
        if name.strip() == "OPENAI_API_KEY":
            key = val.strip().strip('"').strip("'")
            if key:
                return key
    return ""


def docker_env_passing_openai_key(base_env: dict[str, str] | None, key: str) -> dict[str, str]:
    """Env for docker CLI so ``-e OPENAI_API_KEY`` (no =value) picks up the key.

    The key must not appear in argv; callers pass ``-e OPENAI_API_KEY`` only.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env["OPENAI_API_KEY"] = key
    return env


def review_job_id_filters(job_id: str) -> list[str]:
    """Docker filter args scoped to one job (ARB37-001)."""
    jid = (job_id or "").strip()
    if not jid:
        raise ValueError("job_id required for review container filter")
    return ["--filter", f"label=opr.job_id={jid}"]


@dataclass
class ResourceTracker:
    """Track docker resources created by the fixture for reverse-order cleanup."""

    containers: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    review_job_ids: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        if not docker:
            return
        # Review containers for jobs this fixture/test owns (scoped; not global opr.review).
        for job_id in reversed(self.review_job_ids):
            listed = _run([docker, "ps", "-aq", *review_job_id_filters(job_id)])
            for cid in [c.strip() for c in listed.stdout.splitlines() if c.strip()]:
                _rm_container_idempotent(cid)
        for name in reversed(self.containers):
            _rm_container_idempotent(name)
        for name in reversed(self.networks):
            _run([docker, "network", "rm", name])
        for name in reversed(self.volumes):
            _run([docker, "volume", "rm", name])
        for name in reversed(self.images):
            _run([docker, "rmi", "-f", name])


def _assert_secret_absent(blob: str, secret: str, *, where: str) -> None:
    if secret and secret in blob:
        pytest.fail(f"upstream key value leaked into {where} (value not shown)")


def _is_upstream_shaped(value: str) -> bool:
    v = value.strip()
    if v.startswith("OPENAI_API_KEY="):
        rest = v.split("=", 1)[1]
        return rest.startswith("sk-") or rest.startswith("sk_proj")
    return v.startswith("sk-") or v.startswith("sk_proj")


def _published_port(container: str, container_port: int) -> int:
    out = _run([docker, "port", container, str(container_port)])
    assert out.returncode == 0, out.stderr[-300:]
    line = (out.stdout or "").strip().splitlines()[0]
    host_port = line.rsplit(":", 1)[-1].strip()
    return int(host_port)


@pytest.fixture(scope="module")
def upstream_key() -> str:
    key = load_upstream_key_from_model_proxy_env(ROOT / MODEL_PROXY_ENV_NAME)
    if not key:
        pytest.fail(
            f"OPR_REAL_MODEL_E2E is set but no upstream key in {MODEL_PROXY_ENV_NAME}. "
            "Put OPENAI_API_KEY there (never commit that file; ambient OPENAI_API_KEY is ignored)."
        )
    if len(key) < 16:
        pytest.fail("upstream key in model-proxy.env looks too short (value not shown)")
    return key


@pytest.fixture(scope="module")
def real_model_stack(upstream_key: str):
    if not enabled or not docker:
        pytest.skip("Set OPR_REAL_MODEL_E2E=1 with Docker available (manual; not for CI)")

    suffix = uuid.uuid4().hex[:8]
    volume = f"opr_e2e_real_co_{suffix}"
    net_review = f"opr_e2e_real_review_{suffix}"
    net_proxy = f"opr_e2e_real_proxy_{suffix}"
    proxy_name = f"opr-proxy-e2e-{suffix}"
    launcher_name = f"opr-launcher-e2e-{suffix}"
    review_image = f"opr-review:e2e-real-{suffix}"
    launcher_image = f"opr-launcher:e2e-real-{suffix}"
    proxy_image = f"opr-model-proxy:e2e-real-{suffix}"
    issuer = f"e2e-issuer-token-{suffix}-ok"
    launcher_token = f"e2e-launcher-token-{suffix}-ok!!"
    tracker = ResourceTracker()

    try:
        created = _run([docker, "volume", "create", volume])
        assert created.returncode == 0, created.stderr[-300:]
        tracker.volumes.append(volume)

        created = _run([docker, "network", "create", "--internal", net_review])
        assert created.returncode == 0, created.stderr[-300:]
        tracker.networks.append(net_review)

        created = _run([docker, "network", "create", net_proxy])
        assert created.returncode == 0, created.stderr[-300:]
        tracker.networks.append(net_proxy)

        built_review = _run([docker, "build", "-f", str(ROOT / "Dockerfile.review"), "-t", review_image, str(ROOT)])
        assert built_review.returncode == 0, built_review.stderr[-800:]
        tracker.images.append(review_image)

        built_launcher = _run(
            [docker, "build", "-f", str(ROOT / "Dockerfile.launcher"), "-t", launcher_image, str(ROOT)]
        )
        assert built_launcher.returncode == 0, built_launcher.stderr[-800:]
        tracker.images.append(launcher_image)

        built_proxy = _run(
            [
                docker,
                "build",
                "-f",
                str(ROOT / "Dockerfile.model-proxy"),
                "-t",
                proxy_image,
                str(ROOT),
            ]
        )
        assert built_proxy.returncode == 0, built_proxy.stderr[-800:]
        tracker.images.append(proxy_image)

        # ARB37-002: -e OPENAI_API_KEY without =value; key only in subprocess env.
        proxy_cli_env = docker_env_passing_openai_key(os.environ, upstream_key)
        proxy_argv = [
            docker,
            "run",
            "-d",
            "--name",
            proxy_name,
            "--network",
            net_review,
            "-e",
            f"PROXY_ISSUER_TOKEN={issuer}",
            "-e",
            "OPENAI_API_KEY",
            "-e",
            "PROXY_PROVIDER=openai",
            "-e",
            "PROXY_UPSTREAM_HOST=api.openai.com",
            "-p",
            "127.0.0.1::8081",
            proxy_image,
        ]
        assert upstream_key not in " ".join(proxy_argv)
        # ARB37-003: register deterministic name before run so cleanup works if create fails mid-flight.
        tracker.containers.append(proxy_name)
        proxy_run = _run(proxy_argv, env=proxy_cli_env)
        assert proxy_run.returncode == 0, proxy_run.stderr[-500:]

        connect = _run([docker, "network", "connect", net_proxy, proxy_name])
        assert connect.returncode == 0, connect.stderr[-300:]

        # ARB37-003: register deterministic name before run so cleanup works if create fails mid-flight.
        tracker.containers.append(launcher_name)
        launcher_run = _run(
            [
                docker,
                "run",
                "-d",
                "--name",
                launcher_name,
                "--network",
                net_review,
                "-e",
                f"LAUNCHER_TOKEN={launcher_token}",
                "-e",
                "OPR_CHECKOUT_BASE=/var/opr/checkouts",
                "-e",
                f"OPR_CHECKOUT_VOLUME={volume}",
                "-e",
                f"OPR_REVIEW_IMAGE={review_image}",
                "-e",
                f"OPR_REVIEW_NETWORK={net_review}",
                "-e",
                f"OPR_MODEL_PROXY_BASE_URL=http://{proxy_name}:8081/v1",
                "-e",
                "DOCKER_HOST=unix:///var/run/docker.sock",
                "-v",
                "/var/run/docker.sock:/var/run/docker.sock",
                "-v",
                f"{volume}:/var/opr/checkouts",
                "-p",
                "127.0.0.1::8080",
                launcher_image,
            ]
        )
        assert launcher_run.returncode == 0, launcher_run.stderr[-500:]

        proxy_port = _published_port(proxy_name, 8081)
        launcher_port = _published_port(launcher_name, 8080)
        proxy_url = f"http://127.0.0.1:{proxy_port}"
        launcher_url = f"http://127.0.0.1:{launcher_port}"

        deadline = time.time() + 90
        proxy_ok = launcher_ok = False
        while time.time() < deadline:
            try:
                if not proxy_ok and httpx.get(f"{proxy_url}/healthz", timeout=2.0).status_code == 200:
                    proxy_ok = True
                if not launcher_ok and httpx.get(f"{launcher_url}/healthz", timeout=2.0).status_code == 200:
                    launcher_ok = True
                if proxy_ok and launcher_ok:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not (proxy_ok and launcher_ok):
            plogs = _run([docker, "logs", proxy_name]).stdout + _run([docker, "logs", proxy_name]).stderr
            llogs = _run([docker, "logs", launcher_name]).stdout + _run([docker, "logs", launcher_name]).stderr
            _assert_secret_absent(plogs + llogs, upstream_key, where="startup logs")
            pytest.fail("proxy/launcher did not become healthy (logs checked without printing secrets)")

        yield {
            "volume": volume,
            "net_review": net_review,
            "net_proxy": net_proxy,
            "proxy_name": proxy_name,
            "launcher_name": launcher_name,
            "issuer": issuer,
            "launcher_token": launcher_token,
            "launcher_url": launcher_url,
            "proxy_url": proxy_url,
            "review_image": review_image,
            "upstream_key": upstream_key,
            "tracker": tracker,
        }
    finally:
        tracker.cleanup()


def _stage_fixture(volume: str, job_id: str) -> None:
    prompt = (
        f"Return ONLY JSON matching the schema. reviewed_head_sha must be {HEAD_SHA}. "
        "previous_reviewed_head_sha must be null. findings must be []. previous_findings must be []. "
        "task_alignment: issue_key E2E-1, status satisfied, unmet_acceptance_criteria []. "
        "coverage: files_total 1, files_reviewed 1, truncated false, skipped_paths []. "
        "summary: e2e real-model ok. Briefly note /checkout/math_util.py."
    )
    job = {
        "model": DEFAULT_MODEL,
        "timeout_seconds": 180,
        "reasoning_effort": "low",
        "sandbox": "read-only",
        "approval_policy": "never",
        "allow_unsandboxed": False,
    }
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        (stage / "math_util.py").write_text(
            "def add(a, b):\n    return a - b  # intentional off-by-sign for e2e\n",
            encoding="utf-8",
            newline="\n",
        )
        (stage / "prompt.txt").write_text(prompt + "\n", encoding="utf-8", newline="\n")
        (stage / "schema.json").write_text(REVIEW_SCHEMA.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        (stage / "job.json").write_text(json.dumps(job), encoding="utf-8", newline="\n")
        helper = _run(
            [
                docker,
                "run",
                "--rm",
                "-v",
                f"{volume}:/var/opr/checkouts",
                "-v",
                f"{stage}:/stage:ro",
                "busybox:1.36",
                "sh",
                "-c",
                (
                    f"mkdir -p /var/opr/checkouts/{job_id}/repo "
                    f"/var/opr/checkouts/{job_id}/.opr-work && "
                    f"cp /stage/math_util.py /var/opr/checkouts/{job_id}/repo/ && "
                    f"cp /stage/prompt.txt /stage/schema.json /stage/job.json "
                    f"/var/opr/checkouts/{job_id}/"
                ),
            ]
        )
    assert helper.returncode == 0, helper.stderr[-500:]


def _poll_review_env(job_id: str, stop: threading.Event, sink: dict) -> None:
    """Watch for this job's review container Env (scoped by opr.job_id)."""
    filters = review_job_id_filters(job_id)
    while not stop.is_set():
        ps = _run([docker, "ps", "-aq", *filters])
        cids = [c.strip() for c in ps.stdout.splitlines() if c.strip()]
        for cid in cids:
            inspected = _run([docker, "inspect", cid, "--format", "{{json .Config.Env}}"])
            if inspected.returncode != 0:
                continue
            try:
                env_list = json.loads(inspected.stdout.strip() or "[]")
            except json.JSONDecodeError:
                continue
            if not isinstance(env_list, list):
                continue
            sink["env"] = [str(x) for x in env_list]
            sink["cid"] = cid
            cfg = _run([docker, "inspect", cid, "--format", "{{json .Config}}"])
            if cfg.returncode == 0:
                sink["config_json"] = cfg.stdout
            return
        time.sleep(0.2)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not enabled or not docker,
    reason="Set OPR_REAL_MODEL_E2E=1 with Docker available (manual; not for CI)",
)
async def test_real_model_review_via_proxy_happy_path(real_model_stack) -> None:
    """Review through launcher + model-proxy completes; no upstream key in review env/logs."""
    stack = real_model_stack
    upstream_key = stack["upstream_key"]
    tracker: ResourceTracker = stack["tracker"]
    job_id = uuid.uuid4().hex
    tracker.review_job_ids.append(job_id)
    _stage_fixture(stack["volume"], job_id)

    async with httpx.AsyncClient(timeout=30.0) as client:
        issued = await client.post(
            f"{stack['proxy_url']}/internal/jobs/{job_id}/token",
            headers={"Authorization": f"Bearer {stack['issuer']}"},
            json={"timeout_seconds": 180, "owner_id": ""},
        )
    assert issued.status_code == 200, "token issue failed (response body omitted)"
    token_payload = issued.json()
    proxy_token = str(token_payload.get("token") or "")
    assert proxy_token.startswith("opr_job_"), "expected opr_job_ proxy token (value not shown)"
    _assert_secret_absent(proxy_token, upstream_key, where="issued proxy token")

    sink: dict = {}
    stop = threading.Event()
    watcher = threading.Thread(target=_poll_review_env, args=(job_id, stop, sink), daemon=True)
    watcher.start()
    try:
        headers = {
            "Authorization": f"Bearer {stack['launcher_token']}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=240.0) as client:
            response = await client.post(
                f"{stack['launcher_url']}/v1/reviews",
                headers=headers,
                json={
                    "job_id": job_id,
                    "checkout_ref": f"{job_id}/repo",
                    "proxy_token": proxy_token,
                },
            )
    finally:
        stop.set()
        watcher.join(timeout=5)

    assert response.status_code == 200, "launcher review HTTP failed (body omitted to avoid leaks)"
    data = response.json()
    assert data.get("status") == "ok", f"review status={data.get('status')!r} code={data.get('code')!r}"
    output_raw = data.get("output") or ""
    _assert_secret_absent(output_raw, upstream_key, where="review output")
    output = json.loads(output_raw)
    assert output.get("schema_version") == 1
    assert isinstance(output.get("findings"), list)

    env_list = sink.get("env") or []
    assert env_list, "did not observe review container env (container finished too fast?)"
    joined = "\n".join(env_list)
    _assert_secret_absent(joined, upstream_key, where="review container env")
    codex_vals = [e.split("=", 1)[1] for e in env_list if e.startswith("CODEX_API_KEY=")]
    assert codex_vals, "CODEX_API_KEY missing from review container env"
    assert codex_vals[0].startswith("opr_job_"), "CODEX_API_KEY must be an opr_job_ token"
    assert not any(e.startswith("OPENAI_API_KEY=") for e in env_list)
    assert not any(_is_upstream_shaped(e) for e in env_list)

    config_json = sink.get("config_json") or ""
    _assert_secret_absent(config_json, upstream_key, where="launch Config JSON")
    assert "OPENAI_API_KEY=" not in config_json

    for name, where in (
        (stack["proxy_name"], "model-proxy logs"),
        (stack["launcher_name"], "launcher logs"),
    ):
        logs = _run([docker, "logs", name]).stdout + _run([docker, "logs", name]).stderr
        _assert_secret_absent(logs, upstream_key, where=where)

    metrics = httpx.get(f"{stack['proxy_url']}/metrics", timeout=10.0)
    assert metrics.status_code == 200
    _assert_secret_absent(metrics.text, upstream_key, where="model-proxy /metrics")
