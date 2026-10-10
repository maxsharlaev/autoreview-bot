"""Tests for Multi-owner Milestone 2 (M2) features.

This module tests:
- Routing: resolve(full_name, installation_id, explicit_owner) with RouteResult
- Multi-secret HMAC verification (constant-time, no early exit)
- Principal-based HTTP API authentication (operator vs owner-scoped keys)
- X-Review-Owner header / ?owner= selector
- Two owners scenario (App + PAT)
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import MagicMock

from app.config import AppConfig
from app.owners.registry import (
    REJECT_OWNER_DISABLED,
    REJECT_OWNER_REPO_CONFLICT,
    REJECT_REPO_NOT_ALLOWED,
    REJECT_UNKNOWN_OWNER,
    ROUTE_DEFAULT_FALLBACK,
    ROUTE_INSTALLATION,
    ROUTE_WILDCARD,
    OwnerRegistry,
    RouteResult,
)


def _mock_settings(
    github_token: str = "",
    github_app_id: int = 0,
    github_app_private_key: str = "",
    github_installation_id: int = 0,
    github_webhook_secret: str = "",
    openai_api_key: str = "",
    review_api_key: str = "",
) -> MagicMock:
    """Create a mock Settings object."""
    mock = MagicMock()
    mock.github_token = github_token
    mock.github_app_id = github_app_id
    mock.github_app_private_key = github_app_private_key
    mock.github_installation_id = github_installation_id
    mock.github_webhook_secret = github_webhook_secret
    mock.jira_base_url = ""
    mock.jira_email = ""
    mock.jira_api_token = ""
    mock.slack_bot_token = ""
    mock.openai_api_key = openai_api_key
    mock.review_api_key = review_api_key
    mock.github_private_key_pem.return_value = github_app_private_key
    return mock


# --- Routing tests ---


class TestRouteResult:
    """Tests for RouteResult dataclass."""

    def test_rejected_returns_true_for_rejection_reasons(self):
        """RouteResult.rejected() should return True for rejection reasons."""
        assert RouteResult("none", REJECT_UNKNOWN_OWNER).rejected()
        assert RouteResult("none", REJECT_REPO_NOT_ALLOWED).rejected()
        assert RouteResult("none", REJECT_OWNER_REPO_CONFLICT).rejected()
        assert RouteResult("none", REJECT_OWNER_DISABLED).rejected()

    def test_rejected_returns_false_for_success_reasons(self):
        """RouteResult.rejected() should return False for success reasons."""
        assert not RouteResult("org-a", "exact").rejected()
        assert not RouteResult("org-a", ROUTE_WILDCARD).rejected()
        assert not RouteResult("org-a", ROUTE_INSTALLATION).rejected()
        assert not RouteResult("default", ROUTE_DEFAULT_FALLBACK).rejected()


class TestRoutingResolve:
    """Tests for OwnerRegistry.resolve() routing logic."""

    def test_explicit_owner_takes_precedence(self):
        """Explicit owner_id should take precedence when allowed."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-a": {
                    "github": {"auth": "pat", "allowed_repos": ["org-a/*"]},
                },
                "org-b": {
                    "default": True,
                    "github": {"auth": "pat"},  # No allowed_repos = allow all
                },
            }
        )
        env = {
            "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
            "OWNER_ORG_B_GITHUB_TOKEN": "ghp_org_b",
        }

        registry = OwnerRegistry.build(settings, config, env=env)

        # Unclaimed repo with explicit owner should use that owner
        result = registry.resolve("unclaimed-org/repo", explicit_owner="org-b")
        assert result.owner_id == "org-b"
        assert result.reason == "explicit"
        assert not result.rejected()

    def test_explicit_owner_rejected_when_repo_claimed_by_another(self):
        """Explicit owner_id should be rejected when repo is claimed by another owner."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-a": {
                    "github": {"auth": "pat", "allowed_repos": ["org-a/*"]},
                },
                "org-b": {
                    "default": True,
                    "github": {"auth": "pat", "allowed_repos": ["org-b/*"]},
                },
            }
        )
        env = {
            "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
            "OWNER_ORG_B_GITHUB_TOKEN": "ghp_org_b",
        }

        registry = OwnerRegistry.build(settings, config, env=env)

        # org-a/repo is claimed by org-a, so explicit "org-b" should be rejected
        result = registry.resolve("org-a/repo", explicit_owner="org-b")
        assert result.owner_id == ""
        assert result.reason == REJECT_OWNER_REPO_CONFLICT
        assert result.rejected()

    def test_explicit_unknown_owner_is_rejected(self):
        """Explicit owner_id that doesn't exist should be rejected."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-a": {
                    "default": True,
                    "github": {"auth": "pat"},
                },
            }
        )
        env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a"}

        registry = OwnerRegistry.build(settings, config, env=env)

        result = registry.resolve("org/repo", explicit_owner="nonexistent")
        assert result.owner_id == ""  # Empty when rejected
        assert result.reason == REJECT_UNKNOWN_OWNER
        assert result.rejected()

    def test_exact_claim_takes_precedence_over_wildcard(self):
        """Exact repo claim should take precedence over wildcard claim."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-exact": {
                    "github": {"auth": "pat", "allowed_repos": ["org/specific-repo"]},
                },
                "org-wildcard": {
                    "default": True,
                    "github": {"auth": "pat", "allowed_repos": ["org/*"]},
                },
            }
        )
        env = {
            "OWNER_ORG_EXACT_GITHUB_TOKEN": "ghp_exact",
            "OWNER_ORG_WILDCARD_GITHUB_TOKEN": "ghp_wildcard",
        }

        registry = OwnerRegistry.build(settings, config, env=env)

        # org/specific-repo should match exact claim
        result = registry.resolve("org/specific-repo")
        assert result.owner_id == "org-exact"
        assert result.reason == "exact"

        # org/other-repo should match wildcard claim
        result = registry.resolve("org/other-repo")
        assert result.owner_id == "org-wildcard"
        assert result.reason == ROUTE_WILDCARD

    def test_wildcard_claim_routes_correctly(self):
        """Wildcard claims should route correctly."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-a-owner": {
                    "github": {"auth": "pat", "allowed_repos": ["org-a/*"]},
                },
                "org-b-owner": {
                    "default": True,
                    "github": {"auth": "pat", "allowed_repos": ["org-b/*"]},
                },
            }
        )
        env = {
            "OWNER_ORG_A_OWNER_GITHUB_TOKEN": "ghp_a",
            "OWNER_ORG_B_OWNER_GITHUB_TOKEN": "ghp_b",
        }

        registry = OwnerRegistry.build(settings, config, env=env)

        result = registry.resolve("org-a/some-repo")
        assert result.owner_id == "org-a-owner"
        assert result.reason == ROUTE_WILDCARD

        result = registry.resolve("org-b/another-repo")
        assert result.owner_id == "org-b-owner"
        assert result.reason == ROUTE_WILDCARD

    def test_installation_id_claim_routes_correctly(self):
        """Installation ID claim should route when no exact/wildcard match."""
        key = "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "app-owner": {
                    "default": True,
                    "github": {"auth": "app", "app_id": 123, "installation_id": 456},
                },
            }
        )
        env = {"OWNER_APP_OWNER_GITHUB_APP_PRIVATE_KEY": key}

        registry = OwnerRegistry.build(settings, config, env=env)

        # No repo claim, but installation_id matches
        result = registry.resolve("some-org/some-repo", installation_id=456)
        assert result.owner_id == "app-owner"
        assert result.reason == ROUTE_INSTALLATION

    def test_default_fallback_when_no_claims_match(self):
        """Default owner should be used when no claims match."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "default-owner": {
                    "default": True,
                    "github": {"auth": "pat"},
                },
            }
        )
        env = {"OWNER_DEFAULT_OWNER_GITHUB_TOKEN": "ghp_default"}

        registry = OwnerRegistry.build(settings, config, env=env)

        result = registry.resolve("unknown-org/unknown-repo")
        assert result.owner_id == "default-owner"
        assert result.reason == ROUTE_DEFAULT_FALLBACK

    def test_no_match_and_no_default_is_rejected(self):
        """No routing match and no default should be rejected."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-a": {
                    "github": {"auth": "pat", "allowed_repos": ["org-a/*"]},
                },
            }
        )
        env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_a"}

        registry = OwnerRegistry.build(settings, config, env=env)

        result = registry.resolve("other-org/repo")
        assert result.owner_id == ""
        assert result.reason == REJECT_REPO_NOT_ALLOWED
        assert result.rejected()


# --- Multi-secret HMAC verification tests ---


class TestMultiSecretHMAC:
    """Tests for multi-secret HMAC verification."""

    def test_single_secret_match(self):
        """Single secret that matches should return that owner."""
        from app.security.webhook import verify_github_signature_multi

        body = b'{"zen":"test"}'
        secret = "secret1234567890"
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"sha256={digest}"

        secrets = {"org-a": secret}
        result = verify_github_signature_multi(secrets=secrets, body=body, header=header)

        assert result == {"org-a"}

    def test_multiple_secrets_one_match(self):
        """Multiple secrets where one matches should return only that owner."""
        from app.security.webhook import verify_github_signature_multi

        body = b'{"action":"opened"}'
        matching_secret = "matching-secret-16"
        other_secret = "other-secret-12345"

        digest = hmac.new(matching_secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"sha256={digest}"

        secrets = {
            "org-a": other_secret,
            "org-b": matching_secret,
            "org-c": "third-secret-12345",
        }
        result = verify_github_signature_multi(secrets=secrets, body=body, header=header)

        assert result == {"org-b"}

    def test_same_secret_multiple_owners(self):
        """Same secret used by multiple owners should return all of them."""
        from app.security.webhook import verify_github_signature_multi

        body = b'{"shared":"data"}'
        shared_secret = "shared-secret-1234"

        digest = hmac.new(shared_secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"sha256={digest}"

        secrets = {
            "org-a": shared_secret,
            "org-b": shared_secret,
            "org-c": "different-secret-1",
        }
        result = verify_github_signature_multi(secrets=secrets, body=body, header=header)

        assert result == {"org-a", "org-b"}

    def test_no_match_returns_empty_set(self):
        """No matching secrets should return empty set."""
        from app.security.webhook import verify_github_signature_multi

        body = b'{"no":"match"}'
        digest = hmac.new(b"wrong-secret-12345", body, hashlib.sha256).hexdigest()
        header = f"sha256={digest}"

        secrets = {
            "org-a": "secret-a-123456789",
            "org-b": "secret-b-123456789",
        }
        result = verify_github_signature_multi(secrets=secrets, body=body, header=header)

        assert result == set()

    def test_empty_secrets_returns_empty_set(self):
        """Empty secrets dict should return empty set."""
        from app.security.webhook import verify_github_signature_multi

        body = b'{"empty":"secrets"}'
        header = "sha256=abc123"

        result = verify_github_signature_multi(secrets={}, body=body, header=header)

        assert result == set()

    def test_missing_header_returns_empty_set(self):
        """Missing header should return empty set."""
        from app.security.webhook import verify_github_signature_multi

        body = b'{"no":"header"}'
        secrets = {"org-a": "secret-1234567890"}

        result = verify_github_signature_multi(secrets=secrets, body=body, header=None)

        assert result == set()


# --- Principal-based authentication tests ---


class TestPrincipalAuthentication:
    """Tests for Principal-based HTTP API authentication."""

    def test_operator_principal_with_review_api_key(self):
        """REVIEW_API_KEY authenticates as operator principal."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-a": {
                    "default": True,
                    "github": {"auth": "pat"},
                },
            }
        )
        env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a"}

        registry = OwnerRegistry.build(settings, config, env=env)

        kind, owner_id = registry.principal_for_key("operator-key", "operator-key")

        assert kind == "operator"
        assert owner_id is None

    def test_owner_principal_with_owner_key(self):
        """Owner-scoped key authenticates as owner principal."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-a": {
                    "default": True,
                    "github": {"auth": "pat"},
                    "api": {"key_env": "ORG_A_API_KEY"},
                },
            }
        )
        env = {
            "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
            "ORG_A_API_KEY": "org-a-api-key",
        }

        registry = OwnerRegistry.build(settings, config, env=env)

        kind, owner_id = registry.principal_for_key("org-a-api-key", "operator-key")

        assert kind == "owner"
        assert owner_id == "org-a"

    def test_no_match_returns_empty(self):
        """No matching key returns empty kind."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-a": {
                    "default": True,
                    "github": {"auth": "pat"},
                    "api": {"key_env": "ORG_A_API_KEY"},
                },
            }
        )
        env = {
            "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
            "ORG_A_API_KEY": "org-a-api-key",
        }

        registry = OwnerRegistry.build(settings, config, env=env)

        kind, owner_id = registry.principal_for_key("wrong-key", "operator-key")

        assert kind == ""  # Empty string indicates invalid
        assert owner_id is None

    def test_empty_key_returns_empty(self):
        """Empty presented key returns empty kind."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-a": {
                    "default": True,
                    "github": {"auth": "pat"},
                },
            }
        )
        env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a"}

        registry = OwnerRegistry.build(settings, config, env=env)

        kind, owner_id = registry.principal_for_key("", "operator-key")

        assert kind == ""  # Empty string indicates invalid
        assert owner_id is None


# --- Two owners scenario (App + PAT) ---


class TestTwoOwnersScenario:
    """Tests for two owners scenario with different auth methods."""

    def test_app_and_pat_owners_coexist(self):
        """An App-based owner and a PAT-based owner should coexist."""
        key = "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "app-owner": {
                    "github": {
                        "auth": "app",
                        "app_id": 12345,
                        "installation_id": 67890,
                        "allowed_repos": ["app-org/*"],
                    },
                },
                "pat-owner": {
                    "default": True,
                    "github": {
                        "auth": "pat",
                        "allowed_repos": ["pat-org/*"],
                    },
                },
            }
        )
        env = {
            "OWNER_APP_OWNER_GITHUB_APP_PRIVATE_KEY": key,
            "OWNER_PAT_OWNER_GITHUB_TOKEN": "ghp_pat_token",
        }

        registry = OwnerRegistry.build(settings, config, env=env)

        assert len(registry.owners) == 2

        # Verify app owner
        app_ctx = registry.get("app-owner")
        assert app_ctx is not None
        assert app_ctx.github.kind == "app"
        assert app_ctx.github.app_id == 12345
        assert app_ctx.github.installation_id == 67890

        # Verify PAT owner
        pat_ctx = registry.get("pat-owner")
        assert pat_ctx is not None
        assert pat_ctx.github.kind == "pat"
        assert pat_ctx.github.token == "ghp_pat_token"

    def test_routing_between_app_and_pat_owners(self):
        """Routing should correctly direct repos to App vs PAT owners."""
        key = "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "app-owner": {
                    "github": {
                        "auth": "app",
                        "app_id": 12345,
                        "installation_id": 67890,
                        "allowed_repos": ["app-org/*"],
                    },
                },
                "pat-owner": {
                    "default": True,
                    "github": {
                        "auth": "pat",
                    },  # No allowed_repos = accept unclaimed repos as default
                },
            }
        )
        env = {
            "OWNER_APP_OWNER_GITHUB_APP_PRIVATE_KEY": key,
            "OWNER_PAT_OWNER_GITHUB_TOKEN": "ghp_pat_token",
        }

        registry = OwnerRegistry.build(settings, config, env=env)

        # Routing to app-org repos should go to app-owner
        result = registry.resolve("app-org/repo1")
        assert result.owner_id == "app-owner"
        assert result.reason == ROUTE_WILDCARD

        # Unclaimed org should go to default (pat-owner)
        result = registry.resolve("other-org/repo3")
        assert result.owner_id == "pat-owner"
        assert result.reason == ROUTE_DEFAULT_FALLBACK

    def test_webhook_secrets_per_owner(self):
        """Each owner should have their own webhook secret."""
        key = "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "app-owner": {
                    "github": {
                        "auth": "app",
                        "app_id": 12345,
                        "installation_id": 67890,
                    },
                },
                "pat-owner": {
                    "default": True,
                    "github": {"auth": "pat"},
                },
            }
        )
        env = {
            "OWNER_APP_OWNER_GITHUB_APP_PRIVATE_KEY": key,
            "OWNER_APP_OWNER_GITHUB_WEBHOOK_SECRET": "app-secret-12345",
            "OWNER_PAT_OWNER_GITHUB_TOKEN": "ghp_pat_token",
            "OWNER_PAT_OWNER_GITHUB_WEBHOOK_SECRET": "pat-secret-12345",
        }

        registry = OwnerRegistry.build(settings, config, env=env)

        app_ctx = registry.get("app-owner")
        assert app_ctx.webhook_secret == "app-secret-12345"

        pat_ctx = registry.get("pat-owner")
        assert pat_ctx.webhook_secret == "pat-secret-12345"


# --- Owner selector tests ---


class TestOwnerSelector:
    """Tests for X-Review-Owner header / ?owner= query parameter."""

    def test_header_selector(self):
        """X-Review-Owner header should select the owner."""
        from app.api.deps import resolve_owner_selector

        result = resolve_owner_selector(x_review_owner="org-a", owner=None)
        assert result == "org-a"

    def test_query_selector(self):
        """?owner= query parameter should select the owner."""
        from app.api.deps import resolve_owner_selector

        result = resolve_owner_selector(x_review_owner=None, owner="org-b")
        assert result == "org-b"

    def test_header_or_query_works(self):
        """Either header or query parameter should work."""
        from app.api.deps import resolve_owner_selector

        # If both have same value, it should work
        result = resolve_owner_selector(x_review_owner="same-owner", owner="same-owner")
        assert result == "same-owner"

    def test_no_selector_returns_none(self):
        """No selector should return None."""
        from app.api.deps import resolve_owner_selector

        result = resolve_owner_selector(x_review_owner=None, owner=None)
        assert result is None

    def test_header_or_query_returns_available(self):
        """Header or query should return whichever is provided."""
        from app.api.deps import resolve_owner_selector

        result = resolve_owner_selector(x_review_owner=None, owner="org-b")
        assert result == "org-b"

        result = resolve_owner_selector(x_review_owner="org-a", owner=None)
        assert result == "org-a"


# --- Owner API key tests ---


class TestOwnerApiKeys:
    """Tests for owner-scoped API keys."""

    def test_owner_api_key_from_config(self):
        """Owner API key should be read from owner config."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-a": {
                    "default": True,
                    "github": {"auth": "pat"},
                    "api": {"key_env": "ORG_A_API_KEY"},
                },
            }
        )
        env = {
            "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
            "ORG_A_API_KEY": "org-a-api-key-value",
        }

        registry = OwnerRegistry.build(settings, config, env=env)

        ctx = registry.get("org-a")
        assert ctx is not None
        assert ctx.api_key == "org-a-api-key-value"

    def test_owner_api_key_authenticates_for_owner(self):
        """Owner API key should authenticate as owner principal via registry."""
        settings = _mock_settings()
        config = AppConfig(
            owners={
                "org-a": {
                    "default": True,
                    "github": {"auth": "pat"},
                    "api": {"key_env": "ORG_A_API_KEY"},
                },
            }
        )
        env = {
            "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
            "ORG_A_API_KEY": "org-a-api-key",
        }

        registry = OwnerRegistry.build(settings, config, env=env)

        kind, owner_id = registry.principal_for_key("org-a-api-key", "operator-key")

        assert kind == "owner"
        assert owner_id == "org-a"
