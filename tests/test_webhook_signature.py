from __future__ import annotations

import base64
import hashlib
import hmac

from app.security.access import extract_presented_key, verify_api_key
from app.security.webhook import authorize_webhook, verify_github_signature


def _basic(user: str, password: str) -> str:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {raw}"


def test_valid_signature() -> None:
    secret = "super-secret"
    body = b'{"zen":"ok"}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_github_signature(secret=secret, body=body, header=f"sha256={digest}")


def test_invalid_signature() -> None:
    assert not verify_github_signature(secret="a", body=b"{}", header="sha256=deadbeef")


def test_missing_header() -> None:
    assert not verify_github_signature(secret="a", body=b"{}", header=None)


def test_bearer_key() -> None:
    assert extract_presented_key("Bearer secret-key", None) == "secret-key"
    assert verify_api_key("secret-key", "secret-key")
    assert not verify_api_key("secret-key", "other")
    assert not verify_api_key("", "secret-key")


def test_x_api_key_header() -> None:
    assert extract_presented_key(None, "from-header") == "from-header"


def test_basic_auth_password() -> None:
    assert extract_presented_key(_basic("api", "secret-key"), None) == "secret-key"


def test_webhook_requires_api_key() -> None:
    assert not authorize_webhook(
        api_key="k",
        webhook_secret="s",
        authorization=None,
        x_api_key=None,
        body=b"{}",
        signature_header=None,
    )


def test_webhook_bearer_without_hmac() -> None:
    assert authorize_webhook(
        api_key="k",
        webhook_secret="s",
        authorization="Bearer k",
        x_api_key=None,
        body=b"{}",
        signature_header=None,
    )


def test_webhook_hmac_must_match_when_present() -> None:
    body = b'{"ok":true}'
    digest = hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert authorize_webhook(
        api_key="k",
        webhook_secret="s",
        authorization="Bearer k",
        x_api_key=None,
        body=body,
        signature_header=f"sha256={digest}",
    )
    assert not authorize_webhook(
        api_key="k",
        webhook_secret="s",
        authorization="Bearer k",
        x_api_key=None,
        body=body,
        signature_header="sha256=deadbeef",
    )


def test_github_app_basic_plus_hmac() -> None:
    body = b'{"zen":"ok"}'
    digest = hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
    assert authorize_webhook(
        api_key="review-key",
        webhook_secret="hook-secret",
        authorization=_basic("api", "review-key"),
        x_api_key=None,
        body=body,
        signature_header=f"sha256={digest}",
    )
