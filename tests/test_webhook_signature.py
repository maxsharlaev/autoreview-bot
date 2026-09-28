from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from unittest.mock import patch

import pytest
from app.security.access import extract_presented_key, verify_api_key
from app.security.webhook import authorize_webhook, verify_github_signature


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


# --- API key extraction tests ---


def test_bearer_key() -> None:
    assert extract_presented_key("Bearer secret-key", None) == "secret-key"
    assert verify_api_key("secret-key", "secret-key")
    assert not verify_api_key("secret-key", "other")
    assert not verify_api_key("", "secret-key")


def test_x_api_key_header() -> None:
    assert extract_presented_key(None, "from-header") == "from-header"


def test_basic_auth_password() -> None:
    assert extract_presented_key(_basic("api", "secret-key"), None) == "secret-key"


# --- authorize_webhook tests ---


def test_webhook_requires_api_key() -> None:
    body = b'{"zen":"ok"}'
    sig = _make_signature("webhook-secret-16ch", body)
    assert not authorize_webhook(
        api_key="api-key-value",
        webhook_secret="webhook-secret-16ch",
        authorization=None,
        x_api_key=None,
        body=body,
        signature_header=sig,
    )


def test_webhook_requires_signature_header() -> None:
    """Requests without X-Hub-Signature-256 are rejected even with valid API key."""
    assert not authorize_webhook(
        api_key="api-key-value",
        webhook_secret="webhook-secret-16ch",
        authorization="Bearer api-key-value",
        x_api_key=None,
        body=b"{}",
        signature_header=None,
    )


def test_webhook_rejects_wrong_signature() -> None:
    body = b'{"ok":true}'
    assert not authorize_webhook(
        api_key="api-key-value",
        webhook_secret="webhook-secret-16ch",
        authorization="Bearer api-key-value",
        x_api_key=None,
        body=body,
        signature_header="sha256=deadbeef",
    )


def test_webhook_rejects_malformed_signature() -> None:
    body = b'{"ok":true}'
    assert not authorize_webhook(
        api_key="api-key-value",
        webhook_secret="webhook-secret-16ch",
        authorization="Bearer api-key-value",
        x_api_key=None,
        body=body,
        signature_header="sha256deadbeef",
    )


def test_webhook_accepts_valid_signature() -> None:
    body = b'{"ok":true}'
    sig = _make_signature("webhook-secret-16ch", body)
    assert authorize_webhook(
        api_key="api-key-value",
        webhook_secret="webhook-secret-16ch",
        authorization="Bearer api-key-value",
        x_api_key=None,
        body=body,
        signature_header=sig,
    )


def test_github_app_basic_plus_hmac() -> None:
    body = b'{"zen":"ok"}'
    sig = _make_signature("hook-secret-16chars", body)
    assert authorize_webhook(
        api_key="review-key-value",
        webhook_secret="hook-secret-16chars",
        authorization=_basic("api", "review-key-value"),
        x_api_key=None,
        body=body,
        signature_header=sig,
    )


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


# --- Startup validation tests ---


def test_empty_secret_disables_webhook_and_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Empty secret should disable webhook endpoint and log a warning."""
    import app.security.webhook_config as webhook_config
    from app.config import AppConfig, GitHubYaml
    from app.main import _validate_startup_config

    with (
        patch.object(webhook_config, "_webhook_enabled", False),
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


def test_valid_secret_enables_webhook(caplog: pytest.LogCaptureFixture) -> None:
    """Valid secret should enable webhook endpoint without warnings."""
    import app.security.webhook_config as webhook_config
    from app.config import AppConfig, GitHubYaml
    from app.main import _validate_startup_config

    with (
        patch.object(webhook_config, "_webhook_enabled", False),
        patch("app.main.get_settings") as mock_settings,
        patch("app.main.get_app_config") as mock_app_config,
        caplog.at_level(logging.WARNING),
    ):
        mock_settings.return_value.github_webhook_secret = "a-valid-secret-with-16-chars"
        mock_app_config.return_value = AppConfig(github=GitHubYaml(allowed_repos=["org/repo"]))
        _validate_startup_config()
        assert webhook_config._webhook_enabled
        assert "disabled" not in caplog.text.lower()


def test_empty_allowed_repos_logs_warning_when_webhook_enabled(caplog: pytest.LogCaptureFixture) -> None:
    """Empty allowed_repos should log a warning at startup when webhook is enabled."""
    import app.security.webhook_config as webhook_config
    from app.config import AppConfig, GitHubYaml
    from app.main import _validate_startup_config

    with (
        patch.object(webhook_config, "_webhook_enabled", False),
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
    from unittest.mock import MagicMock

    from app.api.v1.pull_request import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)

    mock_session_factory = MagicMock()
    app.state.session_factory = mock_session_factory
    app.state.redis = MagicMock()

    with patch("app.api.v1.pull_request.is_webhook_enabled", return_value=False):
        client = TestClient(app)
        response = client.post(
            "/pull-request",
            headers={"Authorization": "Bearer test-key"},
            json={},
        )
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]
