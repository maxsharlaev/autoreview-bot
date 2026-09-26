from pathlib import Path

import pytest
from app.paths import data_file


def test_prompt_and_schema_are_found() -> None:
    prompt = data_file("prompts", "review.md")
    schema = data_file("schemas", "review_output.json")
    assert prompt.is_file()
    assert schema.is_file()
    assert "Open PR Review" in prompt.read_text(encoding="utf-8")


def test_data_file_uses_override_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nested = tmp_path / "prompts"
    nested.mkdir()
    target = nested / "review.md"
    target.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("OPEN_PR_REVIEW_DATA_DIR", str(tmp_path))
    assert data_file("prompts", "review.md") == target.resolve()
