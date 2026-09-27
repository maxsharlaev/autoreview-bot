from unittest.mock import patch

from app.api.v1.pull_request import classify_pull_request_event
from app.config import AppConfig
from app.services.constants import SKIP_DRAFT, SKIP_FORK


def _payload(**overrides):
    base = {
        "action": "opened",
        "repository": {"full_name": "example-org/example-repo"},
        "pull_request": {
            "number": 1,
            "draft": False,
            "head": {
                "sha": "a" * 40,
                "ref": "ABC-12-feature",
                "repo": {"full_name": "example-org/example-repo", "fork": False},
            },
            "base": {"sha": "b" * 40, "repo": {"full_name": "example-org/example-repo"}},
        },
    }
    base.update(overrides)
    return base


def test_ping_ok() -> None:
    assert classify_pull_request_event("ping", {})["status"] == "ok"


def test_ignores_issue_comment() -> None:
    result = classify_pull_request_event("issue_comment", _payload())
    assert result["reason"] == "ignored_event"


def test_skips_draft() -> None:
    payload = _payload()
    payload["pull_request"]["draft"] = True
    with patch("app.api.v1.pull_request.repo_allowed", return_value=True):
        result = classify_pull_request_event("pull_request", payload)
    assert result["reason"] == SKIP_DRAFT


def test_skips_fork() -> None:
    payload = _payload()
    payload["pull_request"]["head"]["repo"]["fork"] = True
    payload["pull_request"]["head"]["repo"]["full_name"] = "outsider/example-repo"
    with patch("app.api.v1.pull_request.repo_allowed", return_value=True):
        result = classify_pull_request_event("pull_request", payload)
    assert result["reason"] == SKIP_FORK


def test_opened_is_accepted_when_allowlisted() -> None:
    payload = _payload()
    with patch("app.api.v1.pull_request.repo_allowed", return_value=True):
        assert classify_pull_request_event("pull_request", payload) is None


def test_only_override_label_triggers_review() -> None:
    payload = _payload(action="labeled", label={"name": "other"})
    with (
        patch("app.api.v1.pull_request.get_app_config", return_value=AppConfig()),
        patch("app.api.v1.pull_request.repo_allowed", return_value=True),
    ):
        assert classify_pull_request_event("pull_request", payload)["reason"] == "ignored_label"
        payload["label"]["name"] = "autoreview:force"
        assert classify_pull_request_event("pull_request", payload) is None
