"""Hard-coded review container spec invariants."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.launcher.spec import (
    assert_safe_host_config,
    build_container_config,
    build_host_config,
)


def test_host_config_drops_caps_ro_checkout_no_socket(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hi", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    host = build_host_config(checkout=checkout, work_dir=work, prompt=prompt, schema=schema)
    assert "ALL" in host["CapDrop"]
    assert host["Privileged"] is False
    assert any("no-new-privileges" in s for s in host["SecurityOpt"])
    assert host["PidsLimit"] == 256
    assert host["Memory"] > 0
    assert host["NanoCpus"] > 0
    assert any(b.endswith(":/checkout:ro") for b in host["Binds"])
    assert not any("docker.sock" in b for b in host["Binds"])
    assert_safe_host_config(host)


def test_assert_safe_rejects_socket_mount(tmp_path: Path) -> None:
    host = {
        "Binds": ["/var/run/docker.sock:/var/run/docker.sock"],
        "CapDrop": ["ALL"],
        "Privileged": False,
        "SecurityOpt": ["no-new-privileges:true"],
    }
    with pytest.raises(RuntimeError, match="docker socket"):
        assert_safe_host_config(host)


def test_assert_safe_rejects_rw_checkout(tmp_path: Path) -> None:
    host = {
        "Binds": ["/data/repo:/checkout:rw"],
        "CapDrop": ["ALL"],
        "Privileged": False,
        "SecurityOpt": ["no-new-privileges:true"],
    }
    with pytest.raises(RuntimeError, match="read-only"):
        assert_safe_host_config(host)


def test_container_config_labels_and_env(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hi", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    host = build_host_config(checkout=checkout, work_dir=work, prompt=prompt, schema=schema)
    cfg = build_container_config(
        job_id="abc",
        host_config=host,
        model="gpt-test",
        timeout_seconds=30,
        reasoning_effort="low",
        sandbox="read-only",
        approval_policy="never",
        allow_unsandboxed=False,
        model_api_key="sk-test",
    )
    assert cfg["Labels"]["opr.review"] == "1"
    assert cfg["Labels"]["opr.job_id"] == "abc"
    assert "CODEX_API_KEY=sk-test" in cfg["Env"]
    assert cfg["User"] == "1000:1000"
