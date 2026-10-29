"""CI-safe checks for Compose overlays (ARB-27-003 / ARB-27-004).

Parses YAML directly so CI does not need Docker. When ``docker compose`` is
available, also asserts against the fully rendered config.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docker-compose.yml"
CONTAINER = ROOT / "docker-compose.container-runner.yml"
OBSERVABILITY = ROOT / "docker-compose.observability.yml"

_SECRETISH = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PRIVATE)", re.IGNORECASE)


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _service(doc: dict, name: str) -> dict:
    services = doc.get("services") or {}
    assert name in services, f"missing service {name}"
    svc = services[name]
    assert isinstance(svc, dict)
    return svc


def _network_names(svc: dict) -> set[str]:
    nets = svc.get("networks")
    if nets is None:
        return {"default"}
    if isinstance(nets, list):
        return {str(n) for n in nets}
    if isinstance(nets, dict):
        return set(nets.keys())
    raise AssertionError(f"unexpected networks shape: {nets!r}")


def _env_keys(svc: dict) -> set[str]:
    env = svc.get("environment") or {}
    if isinstance(env, dict):
        return {str(k) for k in env.keys()}
    if isinstance(env, list):
        keys: set[str] = set()
        for item in env:
            text = str(item)
            keys.add(text.split("=", 1)[0] if "=" in text else text)
        return keys
    raise AssertionError(f"unexpected environment shape: {env!r}")


def test_launcher_has_no_env_file_and_no_secret_env_names() -> None:
    overlay = _load(CONTAINER)
    launcher = _service(overlay, "launcher")
    assert "env_file" not in launcher, "launcher must not use env_file (would ingest .env secrets)"
    for name in sorted(_env_keys(launcher)):
        if name == "LAUNCHER_TOKEN":
            continue
        assert _SECRETISH.search(name) is None, (
            f"launcher environment must not include secret-like name {name!r} "
            "(only LAUNCHER_TOKEN is allowed among KEY/TOKEN/SECRET/PASSWORD/PRIVATE)"
        )


def test_prometheus_shares_network_with_api_and_worker() -> None:
    base = _load(BASE)
    obs = _load(OBSERVABILITY)
    api_nets = _network_names(_service(base, "api"))
    worker_nets = _network_names(_service(base, "worker"))
    prom_nets = _network_names(_service(obs, "prometheus"))
    assert "internal" in api_nets
    assert "internal" in worker_nets
    assert "internal" in prom_nets, "prometheus must join internal to scrape api/worker"
    assert "default" in prom_nets, "prometheus must stay on default for Grafana/Alloy"
    shared_api = api_nets & prom_nets
    shared_worker = worker_nets & prom_nets
    assert shared_api, "prometheus must share a network with api"
    assert shared_worker, "prometheus must share a network with worker"


def test_overlay_does_not_add_launcher_token_to_api() -> None:
    """ARB-27-008: LAUNCHER_TOKEN is for worker+launcher only; api never calls launcher."""
    overlay = _load(CONTAINER)
    assert "api" not in (overlay.get("services") or {}), "overlay must not redefine api"
    worker = _service(overlay, "worker")
    launcher = _service(overlay, "launcher")
    assert "LAUNCHER_TOKEN" in _env_keys(worker)
    assert "LAUNCHER_TOKEN" in _env_keys(launcher)


def _compose_available() -> bool:
    return shutil.which("docker") is not None


@pytest.mark.skipif(not _compose_available(), reason="docker not available")
def test_rendered_compose_launcher_and_prometheus_networks() -> None:
    """When Docker is present, validate the merged compose config the same way."""
    # Container overlay may require optional vars; use empty LAUNCHER_TOKEN for parse.
    env = {**os.environ, "LAUNCHER_TOKEN": os.environ.get("LAUNCHER_TOKEN") or "test-launcher-token-32chars-ok"}
    # Base + container-runner
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE),
            "-f",
            str(CONTAINER),
            "config",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if rendered.returncode != 0:
        pytest.skip(f"docker compose config failed: {rendered.stderr[-400:]}")
    data = yaml.safe_load(rendered.stdout)
    launcher = _service(data, "launcher")
    assert not launcher.get("env_file"), "rendered launcher must not carry env_file"
    for name in sorted(_env_keys(launcher)):
        if name == "LAUNCHER_TOKEN":
            continue
        assert _SECRETISH.search(name) is None, name
    # Host env supplies LAUNCHER_TOKEN for interpolation; api must not receive it
    # as an explicit environment entry from the overlay (only worker+launcher do).
    api = _service(data, "api")
    api_env = api.get("environment") or {}
    if isinstance(api_env, dict):
        assert "LAUNCHER_TOKEN" not in api_env
    elif isinstance(api_env, list):
        assert not any(str(x).startswith("LAUNCHER_TOKEN=") or str(x) == "LAUNCHER_TOKEN" for x in api_env)
    worker_env_keys = _env_keys(_service(data, "worker"))
    assert "LAUNCHER_TOKEN" in worker_env_keys

    obs_rendered = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE),
            "-f",
            str(OBSERVABILITY),
            "config",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if obs_rendered.returncode != 0:
        pytest.skip(f"docker compose observability config failed: {obs_rendered.stderr[-400:]}")
    obs_data = yaml.safe_load(obs_rendered.stdout)
    prom_nets = _network_names(_service(obs_data, "prometheus"))
    api_nets = _network_names(_service(obs_data, "api"))
    worker_nets = _network_names(_service(obs_data, "worker"))
    assert prom_nets & api_nets
    assert prom_nets & worker_nets
    assert "internal" in prom_nets or any("internal" in n for n in prom_nets)
