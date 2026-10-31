"""Hard-coded review container spec invariants."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.launcher.spec import (
    assert_safe_host_config,
    build_container_config,
    build_host_config,
    load_review_seccomp_profile,
    review_security_opt,
    validate_apparmor_profile,
)

REPO_SECCOMP = Path(__file__).resolve().parents[1] / "docker" / "review" / "seccomp" / "review-bwrap.json"


@pytest.fixture(autouse=True)
def _seccomp_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPR_REVIEW_SECCOMP_PROFILE", str(REPO_SECCOMP))


def test_host_config_drops_caps_ro_checkout_no_socket(tmp_path: Path) -> None:
    host = build_host_config(
        volume_name="opr_checkouts",
        checkout_subpath="job1/repo",
        work_subpath="job1/.opr-work",
        prompt_subpath="job1/prompt.txt",
        schema_subpath="job1/schema.json",
    )
    assert "ALL" in host["CapDrop"]
    assert host["Privileged"] is False
    assert any("no-new-privileges" in s for s in host["SecurityOpt"])
    assert host["PidsLimit"] == 256
    assert host["Memory"] > 0
    assert host["NanoCpus"] > 0
    assert not host.get("Binds")
    mounts = host["Mounts"]
    checkout = next(m for m in mounts if m["Target"] == "/checkout")
    assert checkout["Type"] == "volume"
    assert checkout["Source"] == "opr_checkouts"
    assert checkout["ReadOnly"] is True
    assert checkout["VolumeOptions"]["Subpath"] == "job1/repo"
    assert not any("docker.sock" in str(m).lower() for m in mounts)
    assert any(s.startswith("apparmor=") for s in host["SecurityOpt"])
    assert_safe_host_config(host)


def test_security_opt_uses_embedded_seccomp_never_unconfined(tmp_path: Path) -> None:
    host = build_host_config(
        volume_name="opr_checkouts",
        checkout_subpath="job1/repo",
        work_subpath="job1/.opr-work",
        prompt_subpath="job1/prompt.txt",
        schema_subpath="job1/schema.json",
    )
    joined = " ".join(host["SecurityOpt"]).lower()
    assert "seccomp=unconfined" not in joined
    assert "apparmor=unconfined" in joined
    seccomp = next(s for s in host["SecurityOpt"] if s.startswith("seccomp="))
    body = json.loads(seccomp.removeprefix("seccomp="))
    assert "syscalls" in body
    assert body["defaultAction"] == "SCMP_ACT_ERRNO"
    # Profile is the vendored bwrap-narrowed default (not empty / not unconfined).
    assert any("unshare" in (e.get("names") or []) for e in body["syscalls"])


def _ok_mounts(*, checkout_ro: bool = True) -> list[dict]:
    return [
        {
            "Type": "volume",
            "Source": "opr_checkouts",
            "Target": "/checkout",
            "ReadOnly": checkout_ro,
            "VolumeOptions": {"Subpath": "job1/repo"},
        },
        {
            "Type": "volume",
            "Source": "opr_checkouts",
            "Target": "/job/prompt.txt",
            "ReadOnly": True,
            "VolumeOptions": {"Subpath": "job1/prompt.txt"},
        },
        {
            "Type": "volume",
            "Source": "opr_checkouts",
            "Target": "/job/schema.json",
            "ReadOnly": True,
            "VolumeOptions": {"Subpath": "job1/schema.json"},
        },
        {
            "Type": "volume",
            "Source": "opr_checkouts",
            "Target": "/work",
            "ReadOnly": False,
            "VolumeOptions": {"Subpath": "job1/.opr-work"},
        },
    ]


def test_assert_safe_rejects_binds(tmp_path: Path) -> None:
    host = {
        "Binds": ["/var/run/docker.sock:/var/run/docker.sock"],
        "Mounts": _ok_mounts(),
        "CapDrop": ["ALL"],
        "Privileged": False,
        "SecurityOpt": review_security_opt(),
    }
    with pytest.raises(RuntimeError, match="volume Mounts"):
        assert_safe_host_config(host)


def test_assert_safe_rejects_socket_volume(tmp_path: Path) -> None:
    mounts = _ok_mounts()
    mounts[0] = {
        "Type": "volume",
        "Source": "/var/run/docker.sock",
        "Target": "/checkout",
        "ReadOnly": True,
        "VolumeOptions": {"Subpath": "job1/repo"},
    }
    host = {
        "Mounts": mounts,
        "CapDrop": ["ALL"],
        "Privileged": False,
        "SecurityOpt": review_security_opt(),
    }
    with pytest.raises(RuntimeError, match="docker socket"):
        assert_safe_host_config(host)


def test_assert_safe_rejects_rw_checkout(tmp_path: Path) -> None:
    host = {
        "Mounts": _ok_mounts(checkout_ro=False),
        "CapDrop": ["ALL"],
        "Privileged": False,
        "SecurityOpt": review_security_opt(),
    }
    with pytest.raises(RuntimeError, match="read-only"):
        assert_safe_host_config(host)


def test_assert_safe_rejects_seccomp_unconfined() -> None:
    host = {
        "Mounts": _ok_mounts(),
        "CapDrop": ["ALL"],
        "Privileged": False,
        "SecurityOpt": ["no-new-privileges:true", "seccomp=unconfined"],
    }
    with pytest.raises(RuntimeError, match="seccomp=unconfined"):
        assert_safe_host_config(host)


def test_load_review_seccomp_profile_roundtrip() -> None:
    text = load_review_seccomp_profile(REPO_SECCOMP)
    data = json.loads(text)
    assert data["defaultAction"] == "SCMP_ACT_ERRNO"
    assert isinstance(data["syscalls"], list)


def test_container_config_labels_and_env(tmp_path: Path) -> None:
    host = build_host_config(
        volume_name="opr_checkouts",
        checkout_subpath="job1/repo",
        work_subpath="job1/.opr-work",
        prompt_subpath="job1/prompt.txt",
        schema_subpath="job1/schema.json",
    )
    cfg = build_container_config(
        job_id="abc",
        host_config=host,
        model="gpt-test",
        timeout_seconds=30,
        reasoning_effort="low",
        sandbox="read-only",
        approval_policy="never",
        allow_unsandboxed=False,
        proxy_token="opr_job_test_token_ok",
    )
    assert cfg["Labels"]["opr.review"] == "1"
    assert cfg["Labels"]["opr.job_id"] == "abc"
    assert "CODEX_API_KEY=opr_job_test_token_ok" in cfg["Env"]
    assert not any(e.startswith("OPENAI_API_KEY=") for e in cfg["Env"])
    assert not any("sk-" in e for e in cfg["Env"])
    assert cfg["User"] == "1000:1000"


def test_validate_apparmor_profile_accepts_unconfined_and_names() -> None:
    assert validate_apparmor_profile("unconfined") == "unconfined"
    assert validate_apparmor_profile("docker-default") == "docker-default"
    assert validate_apparmor_profile("opr-review") == "opr-review"


def test_validate_apparmor_profile_rejects_unsafe() -> None:
    import pytest

    for bad in ["", "../etc", "/etc/apparmor.d/usr.bin.foo", "has space", "weird;rm", "a/b"]:
        with pytest.raises(RuntimeError, match="LAUNCHER_APPARMOR_PROFILE"):
            validate_apparmor_profile(bad)


def test_security_opt_honors_launcher_apparmor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCHER_APPARMOR_PROFILE", "opr-review-bwrap")
    opts = review_security_opt()
    assert "apparmor=opr-review-bwrap" in opts
    assert any(s.startswith("seccomp={") for s in opts)


def test_assert_safe_requires_apparmor_opt() -> None:
    host = {
        "Mounts": _ok_mounts(),
        "CapDrop": ["ALL"],
        "Privileged": False,
        "SecurityOpt": ["no-new-privileges:true", "seccomp={}"],
    }
    # Missing apparmor=
    # Need valid seccomp body for earlier checks - use review_security_opt without apparmor by crafting
    opts = [s for s in review_security_opt() if not s.startswith("apparmor=")]
    host["SecurityOpt"] = opts
    with pytest.raises(RuntimeError, match="apparmor="):
        assert_safe_host_config(host)
