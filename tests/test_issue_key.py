from app.services.issue_key import extract_issue_key


def test_extracts_from_branch() -> None:
    assert extract_issue_key("feature/ABC-123-login", "misc") == "ABC-123"


def test_extracts_from_title_when_branch_missing() -> None:
    assert extract_issue_key("feature/login", "ABC-99: add login") == "ABC-99"


def test_none_when_absent() -> None:
    assert extract_issue_key("feature/login", "no ticket") is None
