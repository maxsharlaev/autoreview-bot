"""M4: stack helpers default to container-runner overlay with in-process opt-out."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_stack_ps1_defaults_to_container_overlay_with_inprocess_opt_out() -> None:
    text = (ROOT / "scripts" / "stack.ps1").read_text(encoding="utf-8")
    assert "[switch]$InProcess" in text
    assert "$UseContainerOverlay = -not $InProcess" in text
    assert "docker-compose.container-runner.yml" in text


def test_stack_sh_defaults_to_container_overlay_with_inprocess_opt_out() -> None:
    text = (ROOT / "scripts" / "stack.sh").read_text(encoding="utf-8")
    assert "CONTAINER=1" in text
    assert "--in-process" in text
    assert "docker-compose.container-runner.yml" in text


def test_config_example_defaults_to_container_backend() -> None:
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert "runner_backend: container" in text
