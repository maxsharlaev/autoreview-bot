from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from unittest.mock import MagicMock, patch

import pytest
from app.security.access import extract_presented_key, verify_api_key
from app.security.webhook import verify_github_signature


def _basic(user: str, password: str) -> str:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {raw}"


def _make_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# --- verify_github_signature tests ---


def test_valid_signature() -> None:
    secret = "super-secret"
    body = b'{"zen":"ok"}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(secret=secret, body=body, header=f"sha256={digest}")


def test_invalid_signature() -> None:
    assert not verify_github_signature(secret="a", body=b"{}", header="sha256=deadbeef")


def test_missing_header() -> None:
    assert not verify_github_signature(secret="a", body=b"{}", header=None)


def test_missing_secret() -> None:
    body = b'{"zen":"ok"}'
    digest = hmac.new(b"any-secret", body, hashlib.sha256).hexdigest()
    assert not verify_github_signature(secret="", body=body, header=f"sha256={digest}")


def test_malformed_header_no_equals() -> None:
    assert not verify_github_signature(secret="a", body=b"{}", header="sha256deadbeef")


def test_malformed_header_wrong_algorithm() -> None:
    assert not verify_github_signature(secret="a", body=b"{}", header="sha1=deadbeef")


# --- API key extraction tests (for /api/v1/reviews endpoint) ---


def test_bearer_key() -> None:
    assert extract_presented_key("Bearer secret-key", None) == "secret-key"
    assert verify_api_key("secret-key", "secret-key")
    assert not verify_api_key("secret-key", "other")
    assert not verify_api_key("", "secret-key")


def test_x_api_key_header() -> None:
    assert extract_presented_key(None, "from-header") == "from-header"


def test_basic_auth_password() -> None:
    assert extract_presented_key(_basic("api", "secret-key"), None) == "secret-key"


# --- Webhook endpoint tests (signature only, no API key required) ---


def _create_test_app():
    """Create a test FastAPI app with the webhook router."""
    from app.api.v1.pull_request import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = MagicMock()
    app.state.redis = MagicMock()
    return app, TestClient(app)


def test_webhook_accepts_valid_signature_without_api_key() -> None:
    """Webhook endpoint accepts requests with valid HMAC signature, no API key needed."""
    app, client = _create_test_app()
    secret = "webhook-secret-16ch"
    body = b'{"action":"opened","repository":{"full_name":"org/repo"}}'
    sig = _make_signature(secret, body)

    with (
        patch("app.api.v1.pull_request.is_webhook_enabled", return_value=True),
        patch("app.api.v1.pull_request.get_normalized_secret", return_value=secret),
        patch("app.api.v1.pull_request.repo_allowed", return_value=True),
    ):
        response = client.post(
            "/pull-request",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "ping",
            },
        )
        assert response.status_code in (200, 202), f"Expected 2xx, got {response.status_code}"
        assert response.json()["status"] == "ok"


def test_webhook_rejects_missing_signature() -> None:
    """Webhook endpoint rejects requests without X-Hub-Signature-256 header."""
    app, client = _create_test_app()

    with (
        patch("app.api.v1.pull_request.is_webhook_enabled", return_value=True),
        patch("app.api.v1.pull_request.get_normalized_secret", return_value="webhook-secret-16ch"),
    ):
        response = client.post(
            "/pull-request",
            json={},
            headers={"X-GitHub-Event": "ping"},
        )
        assert response.status_code == 401
        assert "signature" in response.json()["detail"].lower()


def test_webhook_rejects_wrong_signature() -> None:
    """Webhook endpoint rejects requests with invalid HMAC signature."""
    app, client = _create_test_app()

    with (
        patch("app.api.v1.pull_request.is_webhook_enabled", return_value=True),
        patch("app.api.v1.pull_request.get_normalized_secret", return_value="webhook-secret-16ch"),
    ):
        response = client.post(
            "/pull-request",
            json={},
            headers={
                "X-Hub-Signature-256": "sha256=deadbeef",
                "X-GitHub-Event": "ping",
            },
        )
        assert response.status_code == 401
        assert "signature" in response.json()["detail"].lower()


def test_webhook_rejects_malformed_signature() -> None:
    """Webhook endpoint rejects requests with malformed signature header."""
    app, client = _create_test_app()

    with (
        patch("app.api.v1.pull_request.is_webhook_enabled", return_value=True),
        patch("app.api.v1.pull_request.get_normalized_secret", return_value="webhook-secret-16ch"),
    ):
        response = client.post(
            "/pull-request",
            json={},
            headers={
                "X-Hub-Signature-256": "sha256deadbeef",
                "X-GitHub-Event": "ping",
            },
        )
        assert response.status_code == 401


def test_webhook_labeled_autoreview_force_calls_queue_with_force_true() -> None:
    """Labeled event with autoreview:force label queues review with force=True."""
    import json

    app, client = _create_test_app()
    secret = "webhook-secret-16ch"
    payload = {
        "action": "labeled",
        "label": {"name": "autoreview:force"},
        "repository": {"full_name": "org/repo"},
        "pull_request": {
            "number": 42,
            "draft": False,
            "head": {"sha": "abc123", "repo": {"full_name": "org/repo", "fork": False}},
            "base": {"sha": "def456", "repo": {"full_name": "org/repo"}},
        },
    }
    body = json.dumps(payload).encode()
    sig = _make_signature(secret, body)

    mock_run = MagicMock()
    mock_run.status = "queued"
    mock_run.id = "run-123"

    with (
        patch("app.api.v1.pull_request.is_webhook_enabled", return_value=True),
        patch("app.api.v1.pull_request.get_normalized_secret", return_value=secret),
        patch("app.api.v1.pull_request.repo_allowed", return_value=True),
        patch("app.api.v1.pull_request.get_app_config") as mock_config,
        patch("app.api.v1.pull_request.queue_review", return_value=mock_run) as mock_queue,
    ):
        mock_config.return_value.size_guard.override_label = "autoreview:force"
        response = client.post(
            "/pull-request",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
            },
        )
        assert response.status_code in (200, 202), f"Expected 2xx, got {response.status_code}"
        mock_queue.assert_called_once()
        call_kwargs = mock_queue.call_args.kwargs
        assert call_kwargs["force"] is True, "queue_review should be called with force=True"


# --- Webhook secret validation tests ---


def test_is_webhook_secret_valid_rejects_empty() -> None:
    from app.security.webhook_config import is_webhook_secret_valid

    valid, reason = is_webhook_secret_valid("")
    assert not valid
    assert "empty" in reason.lower()


def test_is_webhook_secret_valid_rejects_whitespace() -> None:
    from app.security.webhook_config import is_webhook_secret_valid

    valid, reason = is_webhook_secret_valid("   ")
    assert not valid
    assert "empty" in reason.lower()


def test_is_webhook_secret_valid_rejects_placeholder() -> None:
    from app.security.webhook_config import is_webhook_secret_valid

    for placeholder in ["change-me", "CHANGE-ME", "changeme", "change_me"]:
        valid, reason = is_webhook_secret_valid(placeholder)
        assert not valid, f"Should reject placeholder: {placeholder}"
        assert "placeholder" in reason.lower()


def test_is_webhook_secret_valid_rejects_short_secret() -> None:
    from app.security.webhook_config import WEBHOOK_SECRET_MIN_LENGTH, is_webhook_secret_valid

    short_secret = "a" * (WEBHOOK_SECRET_MIN_LENGTH - 1)
    valid, reason = is_webhook_secret_valid(short_secret)
    assert not valid
    assert "too short" in reason.lower()


def test_is_webhook_secret_valid_accepts_valid_secret() -> None:
    from app.security.webhook_config import WEBHOOK_SECRET_MIN_LENGTH, is_webhook_secret_valid

    valid_secret = "a" * WEBHOOK_SECRET_MIN_LENGTH
    valid, reason = is_webhook_secret_valid(valid_secret)
    assert valid
    assert reason == ""


def test_is_webhook_secret_valid_accepts_long_secret() -> None:
    from app.security.webhook_config import is_webhook_secret_valid

    long_secret = "abcdef1234567890" * 4
    valid, reason = is_webhook_secret_valid(long_secret)
    assert valid
    assert reason == ""


def test_normalize_webhook_secret_strips_whitespace() -> None:
    """Normalized secret should strip leading/trailing whitespace."""
    from app.security.webhook_config import normalize_webhook_secret

    assert normalize_webhook_secret("  secret  ") == "secret"
    assert normalize_webhook_secret("\tsecret\n") == "secret"
    assert normalize_webhook_secret("secret") == "secret"


# --- Startup validation tests ---


def test_empty_secret_disables_webhook_and_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Empty secret should disable webhook endpoint and log a warning."""
    import app.security.webhook_config as webhook_config
    from app.config import AppConfig, GitHubYaml
    from app.main import _validate_startup_config

    with (
        patch.object(webhook_config, "_webhook_enabled", False),
        patch.object(webhook_config, "_normalized_secret", ""),
        patch("app.main.get_settings") as mock_settings,
        patch("app.main.get_app_config") as mock_app_config,
        caplog.at_level(logging.WARNING),
    ):
        mock_settings.return_value.github_webhook_secret = ""
        mock_app_config.return_value = AppConfig(github=GitHubYaml(allowed_repos=["org/repo"]))
        _validate_startup_config()
        assert not webhook_config._webhook_enabled
        assert "empty" in caplog.text.lower()
        assert "disabled" in caplog.text.lower()


def test_placeholder_secret_disables_webhook_and_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Placeholder secret should disable webhook endpoint and log a warning."""
    import app.security.webhook_config as webhook_config
    from app.config import AppConfig, GitHubYaml
    from app.main import _validate_startup_config

    with (
        patch.object(webhook_config, "_webhook_enabled", False),
        patch.object(webhook_config, "_normalized_secret", ""),
        patch("app.main.get_settings") as mock_settings,
        patch("app.main.get_app_config") as mock_app_config,
        caplog.at_level(logging.WARNING),
    ):
        mock_settings.return_value.github_webhook_secret = "change-me"
        mock_app_config.return_value = AppConfig(github=GitHubYaml(allowed_repos=["org/repo"]))
        _validate_startup_config()
        assert not webhook_config._webhook_enabled
        assert "placeholder" in caplog.text.lower()
        assert "disabled" in caplog.text.lower()


def test_short_secret_disables_webhook_and_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Short secret should disable webhook endpoint and log a warning."""
    import app.security.webhook_config as webhook_config
    from app.config import AppConfig, GitHubYaml
    from app.main import _validate_startup_config

    with (
        patch.object(webhook_config, "_webhook_enabled", False),
        patch.object(webhook_config, "_normalized_secret", ""),
        patch("app.main.get_settings") as mock_settings,
        patch("app.main.get_app_config") as mock_app_config,
        caplog.at_level(logging.WARNING),
    ):
        mock_settings.return_value.github_webhook_secret = "tooshort"
        mock_app_config.return_value = AppConfig(github=GitHubYaml(allowed_repos=["org/repo"]))
        _validate_startup_config()
        assert not webhook_config._webhook_enabled
        assert "too short" in caplog.text.lower()
        assert "disabled" in caplog.text.lower()


def test_valid_secret_enables_webhook_and_stores_normalized(caplog: pytest.LogCaptureFixture) -> None:
    """Valid secret should enable webhook endpoint and store normalized secret."""
    import app.security.webhook_config as webhook_config
    from app.config import AppConfig, GitHubYaml
    from app.main import _validate_startup_config

    with (
        patch.object(webhook_config, "_webhook_enabled", False),
        patch.object(webhook_config, "_normalized_secret", ""),
        patch("app.main.get_settings") as mock_settings,
        patch("app.main.get_app_config") as mock_app_config,
        caplog.at_level(logging.WARNING),
    ):
        mock_settings.return_value.github_webhook_secret = "  a-valid-secret-with-16-chars  "
        mock_app_config.return_value = AppConfig(github=GitHubYaml(allowed_repos=["org/repo"]))
        _validate_startup_config()
        assert webhook_config._webhook_enabled
        assert webhook_config._normalized_secret == "a-valid-secret-with-16-chars"
        assert "disabled" not in caplog.text.lower()


def test_empty_allowed_repos_logs_warning_when_webhook_enabled(caplog: pytest.LogCaptureFixture) -> None:
    """Empty allowed_repos should log a warning at startup when webhook is enabled."""
    import app.security.webhook_config as webhook_config
    from app.config import AppConfig, GitHubYaml
    from app.main import _validate_startup_config

    with (
        patch.object(webhook_config, "_webhook_enabled", False),
        patch.object(webhook_config, "_normalized_secret", ""),
        patch("app.main.get_settings") as mock_settings,
        patch("app.main.get_app_config") as mock_app_config,
        caplog.at_level(logging.WARNING),
    ):
        mock_settings.return_value.github_webhook_secret = "a-valid-secret-with-16-chars"
        mock_app_config.return_value = AppConfig(github=GitHubYaml(allowed_repos=[]))
        _validate_startup_config()
        assert "github.allowed_repos is empty" in caplog.text
        assert "any repository" in caplog.text


def test_empty_allowed_repos_no_warning_when_webhook_disabled(caplog: pytest.LogCaptureFixture) -> None:
    """Empty allowed_repos should not warn when webhook is disabled anyway."""
    import app.security.webhook_config as webhook_config
    from app.config import AppConfig, GitHubYaml
    from app.main import _validate_startup_config

    with (
        patch.object(webhook_config, "_webhook_enabled", False),
        patch.object(webhook_config, "_normalized_secret", ""),
        patch("app.main.get_settings") as mock_settings,
        patch("app.main.get_app_config") as mock_app_config,
        caplog.at_level(logging.WARNING),
    ):
        mock_settings.return_value.github_webhook_secret = ""
        mock_app_config.return_value = AppConfig(github=GitHubYaml(allowed_repos=[]))
        _validate_startup_config()
        assert "github.allowed_repos is empty" not in caplog.text


def test_webhook_endpoint_returns_503_when_disabled() -> None:
    """Webhook endpoint should return 503 when secret is not configured."""
    app, client = _create_test_app()

    with patch("app.api.v1.pull_request.is_webhook_enabled", return_value=False):
        response = client.post(
            "/pull-request",
            json={},
        )
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]


# --- /api/v1/reviews endpoint requires API key ---


def test_reviews_endpoint_requires_api_key() -> None:
    """The /api/v1/reviews endpoint requires REVIEW_API_KEY, unlike the webhook."""
    from app.api.v1.reviews import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = MagicMock()
    app.state.redis = MagicMock()

    client = TestClient(app)
    response = client.post(
        "/reviews",
        json={"pull_url": "https://github.com/org/repo/pull/1"},
    )
    assert response.status_code == 401
