"""Unit guards for real-model E2E helpers (no Docker / no real key / no OPR_REAL_MODEL_E2E)."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from tests.test_real_model_e2e import (
    _RM_CONTAINER_MAX_ATTEMPTS,
    CI_INDICATOR_ENV_VARS,
    CI_SKIP_REASON,
    E2E_JOB_TIMEOUT_SECONDS,
    E2E_LABEL,
    E2E_LABEL_VALUE,
    E2E_REAPER_REQUEST_MARGIN_SECONDS,
    E2E_REAPER_STARTUP_MARGIN_SECONDS,
    ResourceTracker,
    _rm_container_idempotent,
    docker_env_passing_openai_key,
    e2e_container_is_stale,
    e2e_reaper_max_age_seconds,
    is_ci_environment,
    load_upstream_key_from_model_proxy_env,
    parse_docker_created_epoch,
    real_model_e2e_skip_reason,
    reap_stale_e2e_containers,
    review_job_id_filters,
    validate_review_output,
)


def test_review_job_id_filters_scoped_to_job() -> None:
    filters = review_job_id_filters("abc123")
    assert filters == ["--filter", "label=opr.job_id=abc123"]
    assert "opr.review" not in " ".join(filters)


def test_review_job_id_filters_reject_empty() -> None:
    with pytest.raises(ValueError):
        review_job_id_filters("")
    with pytest.raises(ValueError):
        review_job_id_filters("   ")


def test_load_upstream_key_ignores_ambient_openai_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient-must-be-ignored-xxxxxxxxxxxx")
    env_file = tmp_path / "model-proxy.env"
    env_file.write_text(
        "OPENAI_API_KEY=sk-from-file-only-yyyyyyyyyyyy\n",
        encoding="utf-8",
        newline="\n",
    )
    assert load_upstream_key_from_model_proxy_env(env_file) == "sk-from-file-only-yyyyyyyyyyyy"


def test_load_upstream_key_missing_file_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient-must-be-ignored-xxxxxxxxxxxx")
    assert load_upstream_key_from_model_proxy_env(tmp_path / "missing.env") == ""


def test_load_upstream_key_missing_key_in_file_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient-must-be-ignored-xxxxxxxxxxxx")
    env_file = tmp_path / "model-proxy.env"
    env_file.write_text("PROXY_PROVIDER=openai\n", encoding="utf-8", newline="\n")
    assert load_upstream_key_from_model_proxy_env(env_file) == ""


def test_docker_env_passes_key_without_putting_it_in_argv() -> None:
    secret = "sk-unit-test-secret-not-for-argv"
    argv = ["docker", "run", "-e", "OPENAI_API_KEY", "img"]
    env = docker_env_passing_openai_key({"PATH": "/usr/bin", "HOME": "/tmp"}, secret)
    assert env["OPENAI_API_KEY"] == secret
    assert secret not in " ".join(argv)
    assert "OPENAI_API_KEY" in argv
    # No =value form on argv
    assert not any(a.startswith("OPENAI_API_KEY=") for a in argv)


def test_resource_tracker_cleanup_order_and_scoped_review_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> Any:
        calls.append(list(args))

        class R:
            returncode = 0
            stdout = "cid-review-1\n" if "ps" in args and "-aq" in args else ""
            stderr = ""

        return R()

    monkeypatch.setattr("tests.test_real_model_e2e.docker", "/usr/bin/docker")
    monkeypatch.setattr("tests.test_real_model_e2e._run", fake_run)

    tracker = ResourceTracker()
    tracker.volumes.append("vol-a")
    tracker.networks.append("net-a")
    tracker.containers.append("proxy-a")
    tracker.images.append("img-a")
    tracker.review_job_ids.append("jobdeadbeef")

    tracker.cleanup()

    # First: scoped review ps by opr.job_id (not global opr.review)
    assert any(
        args[:3] == ["/usr/bin/docker", "ps", "-aq"]
        and "label=opr.job_id=jobdeadbeef" in args
        and "opr.review=1" not in " ".join(args)
        for args in calls
    )
    # Reverse order removals: review cid, then container, network, volume, image
    rm_targets = []
    for args in calls:
        if len(args) >= 4 and args[1] == "rm" and args[2] == "-f":
            rm_targets.append(args[3])
        if len(args) >= 4 and args[1] == "network" and args[2] == "rm":
            rm_targets.append(args[3])
        if len(args) >= 4 and args[1] == "volume" and args[2] == "rm":
            rm_targets.append(args[3])
        if len(args) >= 4 and args[1] == "rmi" and args[2] == "-f":
            rm_targets.append(args[3])
    assert rm_targets == ["cid-review-1", "proxy-a", "net-a", "vol-a", "img-a"]


def test_resource_tracker_cleanup_on_partial_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates setup failure after creating volume+network only."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> Any:
        calls.append(list(args))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr("tests.test_real_model_e2e.docker", "/usr/bin/docker")
    monkeypatch.setattr("tests.test_real_model_e2e._run", fake_run)

    tracker = ResourceTracker()
    try:
        tracker.volumes.append("vol-only")
        tracker.networks.append("net-only")
        raise RuntimeError("simulated setup failure")
    except RuntimeError:
        tracker.cleanup()

    joined = [" ".join(c) for c in calls]
    assert any("volume rm vol-only" in j for j in joined)
    assert any("network rm net-only" in j for j in joined)
    # Nothing else registered
    assert not any("rmi" in j for j in joined)


def test_rm_container_idempotent_ignores_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> Any:
        calls.append(list(args))

        class R:
            returncode = 1
            stdout = ""
            stderr = "Error: No such container: ghost"

        return R()

    monkeypatch.setattr("tests.test_real_model_e2e.docker", "/usr/bin/docker")
    monkeypatch.setattr("tests.test_real_model_e2e._run", fake_run)
    assert _rm_container_idempotent("ghost") is None  # confirmed missing is OK
    assert calls == [["/usr/bin/docker", "rm", "-f", "ghost"]]


def test_tracker_removes_name_registered_before_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARB37-003: name registered before docker run is still cleaned up if run fails."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> Any:
        calls.append(list(args))

        class R:
            returncode = 1 if len(args) > 1 and args[1] == "run" else 0
            stdout = ""
            stderr = "simulated docker run failure" if len(args) > 1 and args[1] == "run" else ""

        return R()

    monkeypatch.setattr("tests.test_real_model_e2e.docker", "/usr/bin/docker")
    monkeypatch.setattr("tests.test_real_model_e2e._run", fake_run)

    from tests import test_real_model_e2e as mod

    tracker = ResourceTracker()
    name = "opr-proxy-e2e-arb37003"
    # Mirror fixture: register BEFORE run.
    tracker.containers.append(name)
    run = mod._run(
        ["/usr/bin/docker", "run", "-d", "--name", name, "img"],
    )
    assert run.returncode != 0
    tracker.cleanup()
    assert any(args == ["/usr/bin/docker", "rm", "-f", name] for args in calls)


def _ci_indicator_sample(name: str) -> str:
    if name == "JENKINS_URL":
        return "https://jenkins.example/"
    if name == "TEAMCITY_VERSION":
        return "2024.1"
    return "true"


@pytest.mark.parametrize("ci_key", list(CI_INDICATOR_ENV_VARS))
def test_flag_plus_ci_skips_real_model_e2e(ci_key: str) -> None:
    """ARB37-006: OPR_REAL_MODEL_E2E=1 still skips when each CI indicator is set."""
    env = {"OPR_REAL_MODEL_E2E": "1", ci_key: _ci_indicator_sample(ci_key)}
    assert is_ci_environment(env)
    reason = real_model_e2e_skip_reason(environ=env, docker_bin="/usr/bin/docker")
    assert reason == CI_SKIP_REASON


def test_flag_without_ci_not_skipped_by_ci_rule() -> None:
    """ARB37-006: with flag and Docker mock, CI rule alone does not skip."""
    env = {"OPR_REAL_MODEL_E2E": "1"}
    assert not is_ci_environment(env)
    assert real_model_e2e_skip_reason(environ=env, docker_bin="/usr/bin/docker") is None


def test_ci_falsey_tokens_do_not_block() -> None:
    env = {"OPR_REAL_MODEL_E2E": "1", "CI": "0", "GITHUB_ACTIONS": "false"}
    assert not is_ci_environment(env)
    assert real_model_e2e_skip_reason(environ=env, docker_bin="/usr/bin/docker") is None


def test_cleanup_continues_after_failure_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARB37-008: attempt every removal, then raise listing failed resource names."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> Any:
        calls.append(list(args))
        joined = " ".join(args)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        if "rm -f ctr-fail" in joined or joined.endswith("rm -f ctr-fail"):
            R.returncode = 1
            R.stderr = "Error: permission denied"
            return R()
        if "rm -f ctr-missing" in joined or joined.endswith("rm -f ctr-missing"):
            R.returncode = 1
            R.stderr = "Error: No such container: ctr-missing"
            return R()
        if "network rm net-fail" in joined:
            R.returncode = 1
            R.stderr = "Error: network is in use"
            return R()
        return R()

    monkeypatch.setattr("tests.test_real_model_e2e.docker", "/usr/bin/docker")
    monkeypatch.setattr("tests.test_real_model_e2e._run", fake_run)
    monkeypatch.setattr("tests.test_real_model_e2e._rm_sleep", lambda _s: None)

    tracker = ResourceTracker()
    tracker.containers.extend(["ctr-fail", "ctr-missing"])
    tracker.networks.extend(["net-fail", "net-ok"])
    tracker.volumes.append("vol-ok")

    with pytest.raises(RuntimeError, match="cleanup failed") as excinfo:
        tracker.cleanup()
    msg = str(excinfo.value)
    assert "container:ctr-fail" in msg
    assert "network:net-fail" in msg
    assert "ctr-missing" not in msg  # confirmed missing is success
    assert "vol-ok" not in msg
    joined = [" ".join(c) for c in calls]
    # Continued after first failure: both containers and both networks attempted.
    assert any(j.endswith("rm -f ctr-fail") for j in joined)
    assert any(j.endswith("rm -f ctr-missing") for j in joined)
    assert any("network rm net-fail" in j for j in joined)
    assert any("network rm net-ok" in j for j in joined)
    assert any("volume rm vol-ok" in j for j in joined)


def test_rm_container_unexpected_failure_returns_label(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(args: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> Any:
        calls.append(list(args))

        class R:
            returncode = 1
            stdout = ""
            stderr = "Error: cannot remove"

        return R()

    monkeypatch.setattr("tests.test_real_model_e2e.docker", "/usr/bin/docker")
    monkeypatch.setattr("tests.test_real_model_e2e._run", fake_run)
    monkeypatch.setattr("tests.test_real_model_e2e._rm_sleep", sleeps.append)
    assert _rm_container_idempotent("stuck") == "container:stuck"
    assert len(calls) == _RM_CONTAINER_MAX_ATTEMPTS
    assert len(sleeps) == _RM_CONTAINER_MAX_ATTEMPTS - 1


def test_validate_review_output_rejects_missing_required_field() -> None:
    """ARB37-007: schema helper rejects output missing a required field."""
    import jsonschema

    incomplete = {
        "schema_version": 1,
        # reviewed_head_sha missing
        "previous_reviewed_head_sha": None,
        "summary": "x",
        "task_alignment": {
            "issue_key": None,
            "status": "satisfied",
            "unmet_acceptance_criteria": [],
        },
        "previous_findings": [],
        "findings": [],
        "coverage": {
            "files_total": 1,
            "files_reviewed": 1,
            "truncated": False,
            "skipped_paths": [],
        },
    }
    with pytest.raises(jsonschema.ValidationError):
        validate_review_output(incomplete)


def test_e2e_container_stale_helper() -> None:
    assert e2e_container_is_stale(created_epoch=0.0, now_epoch=1000.0, max_age_seconds=100)
    assert not e2e_container_is_stale(created_epoch=950.0, now_epoch=1000.0, max_age_seconds=100)


def test_parse_docker_created_epoch_z() -> None:
    ts = parse_docker_created_epoch("2020-01-01T00:00:00.123456789Z")
    assert ts == parse_docker_created_epoch("2020-01-01T00:00:00.123456+00:00")


def test_reaper_removes_only_stale_e2e_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARB37-008 reaper: only stale opr.e2e containers; fresh kept; filter excludes non-e2e."""
    calls: list[list[str]] = []
    # id-stale created long ago; id-fresh recent. Non-e2e never returned by label filter.
    created = {
        "id-stale": "2020-01-01T00:00:00.000000000Z",
        "id-fresh": "2099-01-01T00:00:00.000000000Z",
    }

    def fake_run(args: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> Any:
        calls.append(list(args))
        joined = " ".join(args)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        if "ps" in args and "-aq" in args:
            assert f"label={E2E_LABEL}={E2E_LABEL_VALUE}" in joined
            R.stdout = "id-stale\nid-fresh\n"
            return R()
        if "inspect" in args:
            cid = args[-1]
            R.stdout = created[cid] + "\n"
            return R()
        if "rm" in args and "-f" in args:
            R.returncode = 0
            return R()
        return R()

    monkeypatch.setattr("tests.test_real_model_e2e.docker", "/usr/bin/docker")
    monkeypatch.setattr("tests.test_real_model_e2e._run", fake_run)

    removed = reap_stale_e2e_containers(now_epoch=1_700_000_000.0, max_age_seconds=600)
    assert removed == ["id-stale"]
    joined = [" ".join(c) for c in calls]
    assert any(j.endswith("rm -f id-stale") for j in joined)
    assert not any(j.endswith("rm -f id-fresh") for j in joined)
    # Filter never lists non-e2e ids.
    assert not any("id-other" in j for j in joined)


def test_reaper_ignores_fresh_and_does_not_query_without_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> Any:
        calls.append(list(args))

        class R:
            returncode = 0
            stdout = "fresh-only\n" if "ps" in args else "2099-06-01T12:00:00Z\n"
            stderr = ""

        return R()

    monkeypatch.setattr("tests.test_real_model_e2e.docker", "/usr/bin/docker")
    monkeypatch.setattr("tests.test_real_model_e2e._run", fake_run)
    assert reap_stale_e2e_containers(now_epoch=1_700_000_000.0, max_age_seconds=600) == []
    assert any(f"label={E2E_LABEL}={E2E_LABEL_VALUE}" in " ".join(c) for c in calls)


def test_rm_container_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARB37-008: transient rm failure then success => no error label."""
    calls: list[list[str]] = []
    sleeps: list[float] = []
    n = {"i": 0}

    def fake_run(args: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> Any:
        calls.append(list(args))
        n["i"] += 1

        class R:
            returncode = 1 if n["i"] == 1 else 0
            stdout = ""
            stderr = "Error: busy" if n["i"] == 1 else ""

        return R()

    monkeypatch.setattr("tests.test_real_model_e2e.docker", "/usr/bin/docker")
    monkeypatch.setattr("tests.test_real_model_e2e._run", fake_run)
    monkeypatch.setattr("tests.test_real_model_e2e._rm_sleep", sleeps.append)
    assert _rm_container_idempotent("flaky") is None
    assert len(calls) == 2
    assert len(sleeps) == 1


def test_rm_container_persistent_failure_attempts_then_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARB37-008: persistent failure => 3 attempts and container:<name>."""
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(args: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> Any:
        calls.append(list(args))

        class R:
            returncode = 1
            stdout = ""
            stderr = "Error: still there"

        return R()

    monkeypatch.setattr("tests.test_real_model_e2e.docker", "/usr/bin/docker")
    monkeypatch.setattr("tests.test_real_model_e2e._run", fake_run)
    monkeypatch.setattr("tests.test_real_model_e2e._rm_sleep", sleeps.append)
    assert _rm_container_idempotent("persist") == "container:persist"
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_reaper_threshold_ignores_ambient_max_review_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARB37-009: ambient OPR_MAX_REVIEW_TIMEOUT_SECONDS must not change threshold."""
    monkeypatch.setenv("OPR_MAX_REVIEW_TIMEOUT_SECONDS", "1")
    expected = E2E_JOB_TIMEOUT_SECONDS + E2E_REAPER_STARTUP_MARGIN_SECONDS + E2E_REAPER_REQUEST_MARGIN_SECONDS
    assert e2e_reaper_max_age_seconds() == expected

    calls: list[list[str]] = []
    # Created just under threshold ago relative to now_epoch.
    now = 1_700_000_000.0
    age = expected - 10
    from datetime import datetime

    created = datetime.fromtimestamp(now - age, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")

    def fake_run(args: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> Any:
        calls.append(list(args))

        class R:
            returncode = 0
            stdout = "young-e2e\n" if "ps" in args else created + "\n"
            stderr = ""

        return R()

    monkeypatch.setattr("tests.test_real_model_e2e.docker", "/usr/bin/docker")
    monkeypatch.setattr("tests.test_real_model_e2e._run", fake_run)
    assert reap_stale_e2e_containers(now_epoch=now) == []
    assert not any("rm -f young-e2e" in " ".join(c) for c in calls)
