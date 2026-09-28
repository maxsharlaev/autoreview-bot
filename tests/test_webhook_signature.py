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
    sig = _make_signature("s", body)
    assert not authorize_webhook(
        api_key="k",
        webhook_secret="s",
        authorization=None,
        x_api_key=None,
        body=body,
        signature_header=sig,
    )


def test_webhook_requires_signature_header() -> None:
    """Requests without X-Hub-Signature-256 are rejected even with valid API key."""
    assert not authorize_webhook(
        api_key="k",
        webhook_secret="s",
        authorization="Bearer k",
        x_api_key=None,
        body=b"{}",
        signature_header=None,
    )


def test_webhook_rejects_wrong_signature() -> None:
    body = b'{"ok":true}'
    assert not authorize_webhook(
        api_key="k",
        webhook_secret="s",
        authorization="Bearer k",
        x_api_key=None,
        body=body,
        signature_header="sha256=deadbeef",
    )


def test_webhook_rejects_malformed_signature() -> None:
    body = b'{"ok":true}'
    assert not authorize_webhook(
        api_key="k",
        webhook_secret="s",
        authorization="Bearer k",
        x_api_key=None,
        body=body,
        signature_header="sha256deadbeef",
    )


def test_webhook_accepts_valid_signature() -> None:
    body = b'{"ok":true}'
    sig = _make_signature("s", body)
    assert authorize_webhook(
        api_key="k",
        webhook_secret="s",
        authorization="Bearer k",
        x_api_key=None,
        body=body,
        signature_header=sig,
    )


def test_github_app_basic_plus_hmac() -> None:
    body = b'{"zen":"ok"}'
    sig = _make_signature("hook-secret", body)
    assert authorize_webhook(
        api_key="review-key",
        webhook_secret="hook-secret",
        authorization=_basic("api", "review-key"),
        x_api_key=None,
        body=body,
        signature_header=sig,
    )


# --- Startup validation tests ---


def test_empty_webhook_secret_fails_at_startup() -> None:
    """Application must refuse to start when GITHUB_WEBHOOK_SECRET is empty."""
    from app.main import ConfigurationError, _validate_startup_config

    with patch("app.main.get_settings") as mock_settings:
        mock_settings.return_value.github_webhook_secret = ""
        with pytest.raises(ConfigurationError) as exc_info:
            _validate_startup_config()
        assert "GITHUB_WEBHOOK_SECRET must be set" in str(exc_info.value)


def test_valid_config_starts_successfully() -> None:
    """Application starts when webhook secret is configured."""
    from app.config import AppConfig, GitHubYaml
    from app.main import _validate_startup_config

    with (
        patch("app.main.get_settings") as mock_settings,
        patch("app.main.get_app_config") as mock_app_config,
    ):
        mock_settings.return_value.github_webhook_secret = "valid-secret"
        mock_app_config.return_value = AppConfig(github=GitHubYaml(allowed_repos=["org/repo"]))
        _validate_startup_config()


def test_empty_allowed_repos_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Empty allowed_repos should log a warning at startup."""
    from app.config import AppConfig, GitHubYaml
    from app.main import _validate_startup_config

    with (
        patch("app.main.get_settings") as mock_settings,
        patch("app.main.get_app_config") as mock_app_config,
        caplog.at_level(logging.WARNING),
    ):
        mock_settings.return_value.github_webhook_secret = "valid-secret"
        mock_app_config.return_value = AppConfig(github=GitHubYaml(allowed_repos=[]))
        _validate_startup_config()
        assert "github.allowed_repos is empty" in caplog.text
        assert "any repository" in caplog.text
