"""Root session fingerprint: argon2-derived key + HMAC (no fast pepper verifier)."""

from __future__ import annotations

import pytest
from app.config import Settings
from app.services import admin_root_crypto as crypto_mod
from app.services.admin_config import AdminApiConfigError, validate_admin_env
from app.services.admin_passwords import MIN_ROOT_PASSWORD_LENGTH
from app.services.admin_root_crypto import (
    clear_root_fingerprint_cache,
    compute_root_fingerprint,
    get_root_fingerprint,
)


def test_no_fixed_pepper_fast_verifier_constant() -> None:
    assert not hasattr(crypto_mod, "_ROOT_FINGERPRINT_PEPPER")
    src = __import__("inspect").getsource(crypto_mod)
    assert "_ROOT_FINGERPRINT_PEPPER" not in src
    assert "opr-admin-root-fingerprint-v1" not in src  # old pepper string gone
    assert "hash_secret_raw" in src


def test_fingerprint_changes_with_password() -> None:
    clear_root_fingerprint_cache()
    a = compute_root_fingerprint("root", "password-sixteen1")
    b = compute_root_fingerprint("root", "password-sixteen2")
    assert a != b
    assert len(a) == 64


def test_get_root_fingerprint_caches() -> None:
    clear_root_fingerprint_cache()
    first = get_root_fingerprint("root", "password-sixteen1")
    second = get_root_fingerprint("root", "password-sixteen1")
    assert first == second
    third = get_root_fingerprint("root", "password-sixteen2")
    assert third != first


def test_root_password_min_16_at_startup() -> None:
    settings = Settings(
        admin_api_enabled=True,
        admin_root_login="root",
        admin_root_password="short-but-12ch",  # 14 chars < 16
    )
    with pytest.raises(AdminApiConfigError, match=str(MIN_ROOT_PASSWORD_LENGTH)):
        validate_admin_env(settings)
    settings = Settings(
        admin_api_enabled=True,
        admin_root_login="root",
        admin_root_password="sixteen-chars-ok",  # 16
    )
    validate_admin_env(settings)
    clear_root_fingerprint_cache()
