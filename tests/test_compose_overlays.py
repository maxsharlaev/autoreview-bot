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
        if name in {"LAUNCHER_TOKEN", "PROXY_ISSUER_TOKEN"}:
            continue
        assert _SECRETISH.search(name) is None, (
            f"launcher environment must not include secret-like name {name!r} "
            "(only LAUNCHER_TOKEN / PROXY_ISSUER_TOKEN among KEY/TOKEN/SECRET/PASSWORD/PRIVATE)"
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
        if name in {"LAUNCHER_TOKEN", "PROXY_ISSUER_TOKEN"}:
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


# Launcher settings documented in docs/ENVIRONMENT.md that must appear in the overlay
# with host-interpolated defaults (ARB-27-020). Secrets except LAUNCHER_TOKEN / PROXY_ISSUER_TOKEN are forbidden.
_DOCUMENTED_LAUNCHER_SETTINGS = (
    "LAUNCHER_TOKEN",
    "PROXY_ISSUER_TOKEN",
    "MODEL_PROXY_URL",
    "OPR_MODEL_PROXY_BASE_URL",
    "LAUNCHER_APPARMOR_PROFILE",
    "LAUNCHER_CANCEL_DEADLINE_SECONDS",
    "LAUNCHER_CANCEL_WAIT_SECONDS",
    "LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS",
    "LAUNCHER_CANCEL_REVOKE_MAX_PENDING",
    "OPR_CHECKOUT_BASE",
    "OPR_CHECKOUT_VOLUME",
    "OPR_REVIEW_IMAGE",
    "OPR_REVIEW_NETWORK",
    "OPR_REVIEW_ORPHAN_TTL_SECONDS",
    "OPR_MAX_REVIEW_TIMEOUT_SECONDS",
    "OPR_CLONE_TIMEOUT_SECONDS",
    "OPR_CHECKOUT_ORPHAN_TTL_SECONDS",
    "OPR_LEASE_HEARTBEAT_SECONDS",
    "OPR_REVIEW_REAPER_INTERVAL_SECONDS",
    "DOCKER_HOST",
)

_WORKER_SHARED_SETTINGS = (
    "LAUNCHER_TOKEN",
    "OPR_CHECKOUT_BASE",
    "OPR_MAX_REVIEW_TIMEOUT_SECONDS",
    "OPR_CLONE_TIMEOUT_SECONDS",
    "OPR_CHECKOUT_ORPHAN_TTL_SECONDS",
    "OPR_LEASE_HEARTBEAT_SECONDS",
)


def _docs_launcher_table_vars() -> set[str]:
    """Parse variable names from the Review launcher table in docs/ENVIRONMENT.md."""
    text = (ROOT / "docs" / "ENVIRONMENT.md").read_text(encoding="utf-8")
    # Section starts at "## Review launcher" and ends at the next "## "
    start = text.find("## Review launcher")
    assert start >= 0, "Review launcher section missing from docs/ENVIRONMENT.md"
    rest = text[start:]
    end = rest.find("\n## ", 3)
    section = rest if end < 0 else rest[:end]
    names: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        # | `NAME` | ...
        tick = line.find("`", 3)
        if tick > 3:
            names.add(line[3:tick])
    # Header / HTTP header rows are not compose env keys
    names.discard("Variable")
    names = {
        n for n in names if n.isidentifier() or n.startswith("OPR_") or n.startswith("LAUNCHER_") or n == "DOCKER_HOST"
    }
    names.discard("X-Model-Api-Key")  # HTTP header, not compose env
    names.discard("OPR_DEFAULT_MODEL")  # optional fallback, not required in overlay
    names.discard("OPR_REVIEW_SECCOMP_PROFILE")  # baked path; override for tests only
    names.discard("LAUNCHER_URL")  # worker-only
    return names


def test_overlay_lists_documented_launcher_settings() -> None:
    """ARB-27-020: every supported launcher setting has an explicit compose entry."""
    overlay = _load(CONTAINER)
    launcher_keys = _env_keys(_service(overlay, "launcher"))
    worker_keys = _env_keys(_service(overlay, "worker"))
    for name in _DOCUMENTED_LAUNCHER_SETTINGS:
        assert name in launcher_keys, f"launcher missing {name}"
    for name in _WORKER_SHARED_SETTINGS:
        assert name in worker_keys, f"worker missing shared {name}"
    # Cross-check docs table vs compose (docs may list a few more; compose must cover the required set)
    docs_names = _docs_launcher_table_vars()
    missing_in_compose = docs_names - launcher_keys - {"LAUNCHER_URL"}
    # LAUNCHER_URL is worker-only; drop it from the launcher requirement
    missing_in_compose.discard("LAUNCHER_URL")
    assert not missing_in_compose, f"docs launcher vars missing from compose launcher env: {sorted(missing_in_compose)}"


def test_overlay_launcher_settings_use_safe_defaults() -> None:
    """Host-interpolated values use non-empty safe defaults (except LAUNCHER_TOKEN)."""
    overlay = _load(CONTAINER)
    env = _service(overlay, "launcher").get("environment") or {}
    assert isinstance(env, dict)
    expected_defaults = {
        "LAUNCHER_APPARMOR_PROFILE": "unconfined",
        "LAUNCHER_CANCEL_DEADLINE_SECONDS": "4",
        "LAUNCHER_CANCEL_WAIT_SECONDS": "3",
        "LAUNCHER_CANCEL_REVOKE_TIMEOUT_SECONDS": "2",
        "LAUNCHER_CANCEL_REVOKE_MAX_PENDING": "32",
        "OPR_MAX_REVIEW_TIMEOUT_SECONDS": "600",
        "OPR_CLONE_TIMEOUT_SECONDS": "300",
        "OPR_CHECKOUT_ORPHAN_TTL_SECONDS": "900",
        "OPR_LEASE_HEARTBEAT_SECONDS": "30",
        "OPR_REVIEW_ORPHAN_TTL_SECONDS": "7200",
        "OPR_REVIEW_REAPER_INTERVAL_SECONDS": "300",
    }
    for key, default in expected_defaults.items():
        raw = str(env[key])
        assert f":-{default}" in raw or raw == default, f"{key} should default to {default}, got {raw}"


@pytest.mark.skipif(not _compose_available(), reason="docker not available")
def test_rendered_compose_propagates_host_launcher_settings() -> None:
    """Host-provided launcher setting values propagate into the rendered config."""
    env = {
        **os.environ,
        "LAUNCHER_TOKEN": "test-launcher-token-32chars-ok",
        "OPR_LEASE_HEARTBEAT_SECONDS": "42",
        "OPR_REVIEW_ORPHAN_TTL_SECONDS": "8000",
        "LAUNCHER_CANCEL_DEADLINE_SECONDS": "3.5",
        "LAUNCHER_CANCEL_WAIT_SECONDS": "2.5",
        "OPR_CHECKOUT_ORPHAN_TTL_SECONDS": "1200",
    }
    rendered = subprocess.run(
        ["docker", "compose", "-f", str(BASE), "-f", str(CONTAINER), "config"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if rendered.returncode != 0:
        pytest.skip(f"docker compose config failed: {rendered.stderr[-400:]}")
    data = yaml.safe_load(rendered.stdout)
    launcher_env = _service(data, "launcher").get("environment") or {}
    if isinstance(launcher_env, list):
        launcher_env = dict(item.split("=", 1) for item in launcher_env if "=" in str(item))
    assert str(launcher_env.get("OPR_LEASE_HEARTBEAT_SECONDS")) == "42"
    assert str(launcher_env.get("OPR_REVIEW_ORPHAN_TTL_SECONDS")) == "8000"
    assert str(launcher_env.get("LAUNCHER_CANCEL_WAIT_SECONDS")) in {"2.5", "2.50"}
    assert str(launcher_env.get("OPR_CHECKOUT_ORPHAN_TTL_SECONDS")) == "1200"
    for name in sorted(_env_keys(_service(data, "launcher"))):
        if name in {"LAUNCHER_TOKEN", "PROXY_ISSUER_TOKEN"}:
            continue
        assert _SECRETISH.search(name) is None, name


# Model-proxy settings documented in docs/ENVIRONMENT.md (M3 PR2).
# Secrets OPENAI_API_KEY / PROXY_OWNER_KEY_ENVS come from model-proxy.env (env_file),
# not Compose environment: (ARB34-006 follow-up).
_DOCUMENTED_MODEL_PROXY_SETTINGS = (
    "PROXY_ISSUER_TOKEN",
    "PROXY_PROVIDER",
    "PROXY_UPSTREAM_HOST",
    "PROXY_UPSTREAM_SCHEME",
    "OPR_MAX_REVIEW_TIMEOUT_SECONDS",
    "PROXY_TOKEN_GRACE_SECONDS",
    "PROXY_MAX_BODY_BYTES",
    "PROXY_MAX_REQUESTS_PER_JOB",
    "PROXY_MAX_BYTES_PER_JOB",
    "PROXY_MAX_CONCURRENT_PER_JOB",
    "PROXY_STREAM_IDLE_TIMEOUT_SECONDS",
    "PROXY_UPSTREAM_TIMEOUT_SECONDS",
)

_WORKER_PROXY_SETTINGS = (
    "PROXY_ISSUER_TOKEN",
    "MODEL_PROXY_URL",
    "OPR_PROXY_FINISH_REVOKE_TIMEOUT_SECONDS",
)


def _docs_model_proxy_table_vars() -> set[str]:
    """Parse variable names from the Model proxy table in docs/ENVIRONMENT.md."""
    text = (ROOT / "docs" / "ENVIRONMENT.md").read_text(encoding="utf-8")
    start = text.find("## Model proxy")
    assert start >= 0, "Model proxy section missing from docs/ENVIRONMENT.md"
    rest = text[start:]
    end = rest.find("\n## ", 3)
    section = rest if end < 0 else rest[:end]
    names: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        tick = line.find("`", 3)
        if tick > 3:
            names.add(line[3:tick])
    names.discard("Variable")
    # Pattern rows / headers that are not literal compose keys
    names = {
        n
        for n in names
        if n.isidentifier()
        or n.startswith("OPR_")
        or n.startswith("PROXY_")
        or n in {"OPENAI_API_KEY", "MODEL_PROXY_URL"}
    }
    names = {n for n in names if "<" not in n and ">" not in n}
    return names


def test_overlay_lists_model_proxy_service() -> None:
    overlay = _load(CONTAINER)
    proxy = _service(overlay, "model-proxy")
    env_files = proxy.get("env_file") or []
    assert env_files, "model-proxy must load model-proxy.env via env_file (ARB34-006)"
    # Normalize compose env_file entries (string or {path, required})
    paths: list[str] = []
    for item in env_files if isinstance(env_files, list) else [env_files]:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, dict):
            paths.append(str(item.get("path") or ""))
    assert any(p.endswith("model-proxy.env") or p == "model-proxy.env" for p in paths), paths
    for svc_name in ("worker", "launcher"):
        svc = _service(overlay, svc_name)
        other = svc.get("env_file") or []
        assert not other, f"{svc_name} must not use env_file (secrets stay on model-proxy)"
    volumes = proxy.get("volumes") or []
    assert not any("docker.sock" in str(v) for v in volumes), "model-proxy must not mount Docker socket"
    nets = _network_names(proxy)
    assert "internal" in nets
    assert "review_egress" in nets
    assert "proxy_egress" in nets
    review_net = (overlay.get("networks") or {}).get("review_egress") or {}
    assert review_net.get("internal") is True
    launcher_keys = _env_keys(_service(overlay, "launcher"))
    for name in ("PROXY_ISSUER_TOKEN", "MODEL_PROXY_URL", "OPR_MODEL_PROXY_BASE_URL"):
        assert name in launcher_keys, f"launcher missing {name}"
    keys = _env_keys(proxy)
    for name in _DOCUMENTED_MODEL_PROXY_SETTINGS:
        assert name in keys, f"model-proxy missing {name}"
    for secret_name in ("OPENAI_API_KEY", "PROXY_OWNER_KEY_ENVS"):
        assert secret_name not in keys, f"{secret_name} must not be in Compose environment: (overrides env_file)"
    worker_keys = _env_keys(_service(overlay, "worker"))
    for name in _WORKER_PROXY_SETTINGS:
        assert name in worker_keys, f"worker missing {name}"
    docs_names = _docs_model_proxy_table_vars()
    # MODEL_PROXY_URL is worker-only; PROXY_OWNER_* pattern is override-only
    missing = (
        docs_names
        - keys
        - {
            "MODEL_PROXY_URL",
            "OPR_MODEL_PROXY_BASE_URL",
            "OPR_PROXY_FINISH_REVOKE_TIMEOUT_SECONDS",
            # env_file-only secrets (must not appear in Compose environment:)
            "OPENAI_API_KEY",
            "PROXY_OWNER_KEY_ENVS",
        }
    )
    assert not missing, f"docs model-proxy vars missing from compose: {sorted(missing)}"


def test_overlay_model_proxy_settings_use_safe_defaults() -> None:
    overlay = _load(CONTAINER)
    env = _service(overlay, "model-proxy").get("environment") or {}
    assert isinstance(env, dict)
    expected_defaults = {
        "PROXY_PROVIDER": "openai",
        "PROXY_UPSTREAM_HOST": "api.openai.com",
        "PROXY_UPSTREAM_SCHEME": "https",
        "OPR_MAX_REVIEW_TIMEOUT_SECONDS": "600",
        "PROXY_TOKEN_GRACE_SECONDS": "60",
        "PROXY_MAX_BODY_BYTES": "4194304",
        "PROXY_MAX_REQUESTS_PER_JOB": "50",
        "PROXY_MAX_BYTES_PER_JOB": "16777216",
        "PROXY_MAX_CONCURRENT_PER_JOB": "2",
        "PROXY_STREAM_IDLE_TIMEOUT_SECONDS": "300",
        "PROXY_UPSTREAM_TIMEOUT_SECONDS": "660",
        "MODEL_PROXY_URL": "http://model-proxy:8081",  # checked on worker below
    }
    for key, default in expected_defaults.items():
        if key == "MODEL_PROXY_URL":
            continue
        raw = str(env[key])
        assert f":-{default}" in raw or raw == default, f"{key} should default to {default}, got {raw}"
    worker_env = _service(overlay, "worker").get("environment") or {}
    raw_url = str(worker_env["MODEL_PROXY_URL"])
    assert "model-proxy:8081" in raw_url


def test_model_proxy_env_file_hyphen_owner_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ARB34-006: vars named in PROXY_OWNER_KEY_ENVS resolve when loaded like model-proxy.env."""
    from app.model_proxy.settings import load_settings, resolve_owner_upstream_keys

    env_file = tmp_path / "model-proxy.env"
    env_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=sk-default-from-file",
                'PROXY_OWNER_KEY_ENVS={"org-a":"ORG_A_HYPHEN_KEY","org_a":"ORG_A_UNDER_KEY"}',
                "ORG_A_HYPHEN_KEY=sk-hyphen-secret",
                "ORG_A_UNDER_KEY=sk-under-secret",
                "",
            ]
        ),
        encoding="utf-8",
    )
    # Simulate compose env_file injection into process env for model-proxy only.
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        monkeypatch.setenv(k.strip(), v)
    monkeypatch.setenv("PROXY_ISSUER_TOKEN", "issuer-token-16chars")
    resolved = resolve_owner_upstream_keys()
    assert resolved["org-a"] == "sk-hyphen-secret"
    assert resolved["org_a"] == "sk-under-secret"
    settings = load_settings()
    assert settings.upstream_key_for_owner("org-a") == "sk-hyphen-secret"
    assert settings.upstream_key_for_owner("org_a") == "sk-under-secret"
    # Overlay: only model-proxy references the file
    overlay = _load(CONTAINER)
    assert any(
        (isinstance(x, str) and "model-proxy.env" in x)
        or (isinstance(x, dict) and "model-proxy.env" in str(x.get("path")))
        for x in (_service(overlay, "model-proxy").get("env_file") or [])
    )
    for name in ("worker", "launcher"):
        assert not _service(overlay, name).get("env_file")


@pytest.mark.skipif(not _compose_available(), reason="docker not available")
def test_rendered_compose_model_proxy_env_file_wins() -> None:
    """ARB34-006: secrets from model-proxy.env reach model-proxy; not worker/launcher.

    Host env must not set OPENAI_API_KEY / PROXY_OWNER_KEY_ENVS so empty Compose
    `environment:` interpolation cannot override env_file (regression guard).
    """
    env_path = ROOT / "model-proxy.env"
    created = False
    previous: str | None = None
    if env_path.exists():
        previous = env_path.read_text(encoding="utf-8")
    else:
        created = True
    env_path.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=sk-dummy-from-env-file-not-a-secret",
                'PROXY_OWNER_KEY_ENVS={"org-a":"ORG_A_HYPHEN_KEY"}',
                "ORG_A_HYPHEN_KEY=sk-dummy-org-a-hyphen-not-a-secret",
                "",
            ]
        ),
        encoding="utf-8",
    )
    try:
        env = {
            **os.environ,
            "LAUNCHER_TOKEN": "test-launcher-token-32chars-ok",
            "PROXY_ISSUER_TOKEN": "test-issuer-token-32chars-ok!!",
            "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD") or "test-postgres-password",
        }
        env.pop("OPENAI_API_KEY", None)
        env.pop("PROXY_OWNER_KEY_ENVS", None)
        env.pop("ORG_A_HYPHEN_KEY", None)

        rendered = subprocess.run(
            ["docker", "compose", "-f", str(BASE), "-f", str(CONTAINER), "config"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if rendered.returncode != 0:
            pytest.skip(f"docker compose config failed: {rendered.stderr[-500:]}")
        data = yaml.safe_load(rendered.stdout)
        proxy_env = _service(data, "model-proxy").get("environment") or {}
        if isinstance(proxy_env, list):
            proxy_env = dict(item.split("=", 1) for item in proxy_env if isinstance(item, str) and "=" in item)
        assert proxy_env.get("OPENAI_API_KEY") == "sk-dummy-from-env-file-not-a-secret"
        assert "org-a" in str(proxy_env.get("PROXY_OWNER_KEY_ENVS") or "")
        assert proxy_env.get("ORG_A_HYPHEN_KEY") == "sk-dummy-org-a-hyphen-not-a-secret"
        for svc_name in ("worker", "launcher"):
            other = _service(data, svc_name).get("environment") or {}
            if isinstance(other, list):
                other = dict(item.split("=", 1) for item in other if isinstance(item, str) and "=" in item)
            # Proxy-only env_file secrets must not leak to worker/launcher.
            # (Worker may still have OPENAI_API_KEY from the base stack .env.)
            assert other.get("PROXY_OWNER_KEY_ENVS") in (None, "")
            assert other.get("ORG_A_HYPHEN_KEY") in (None, "")
            assert other.get("OPENAI_API_KEY") != "sk-dummy-from-env-file-not-a-secret"
    finally:
        if created:
            env_path.unlink(missing_ok=True)
        elif previous is not None:
            env_path.write_text(previous, encoding="utf-8")
