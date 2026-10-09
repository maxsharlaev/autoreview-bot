"""Tests for webhook event classification logic.

classify_pull_request_event only handles event type / action filtering and
PR eligibility (draft, fork). Repo allowlist checks happen after routing.
"""

from app.api.v1.pull_request import classify_pull_request_event
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
    result = classify_pull_request_event("pull_request", payload)
    assert result["reason"] == SKIP_DRAFT


def test_skips_fork() -> None:
    payload = _payload()
    payload["pull_request"]["head"]["repo"]["fork"] = True
    payload["pull_request"]["head"]["repo"]["full_name"] = "outsider/example-repo"
    result = classify_pull_request_event("pull_request", payload)
    assert result["reason"] == SKIP_FORK


def test_opened_is_accepted_when_allowlisted() -> None:
    """Opened event on non-draft, non-fork PR returns None (accept)."""
    payload = _payload()
    assert classify_pull_request_event("pull_request", payload) is None


def test_only_override_label_triggers_review() -> None:
    """Labeled event must match override_label to trigger review."""
    payload = _payload(action="labeled", label={"name": "other"})
    # Uses default override_label from AppConfig ("autoreview:force")
    assert classify_pull_request_event("pull_request", payload)["reason"] == "ignored_label"
    payload["label"]["name"] = "autoreview:force"
    assert classify_pull_request_event("pull_request", payload) is None
