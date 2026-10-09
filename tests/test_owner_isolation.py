"""Tests for owner context isolation.

These tests verify that owner-specific clients do NOT inherit global settings,
preventing cross-owner data leaks.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import respx
from app.adapters.github import GitHubAppClient, _clear_caches
from app.adapters.jira import JiraClient
from app.adapters.slack import SlackClient
from app.config import get_app_config, get_settings
from app.owners.context import GitHubCredentials, JiraBinding, OwnerContext, SlackBinding
from app.services.publisher import Publisher
from httpx import Response


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear GitHub and settings caches before each test."""
    _clear_caches()
    get_settings.cache_clear()
    yield
    _clear_caches()
    get_settings.cache_clear()


class TestPublisherOwnerIsolation:
    """Tests that Publisher.from_context() doesn't leak to global Jira/Slack."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_jira_slack_binding_makes_zero_requests(self, monkeypatch):
        """Owner context with jira=None and slack=None should not make any requests
        to Jira or Slack hosts, even when global JIRA_* and SLACK_BOT_TOKEN are set.
        """
        # Set global settings that would be used if we incorrectly inherited
        monkeypatch.setenv("JIRA_BASE_URL", "https://global-jira.atlassian.net")
        monkeypatch.setenv("JIRA_EMAIL", "global@example.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "global-token")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-global-token")

        # Clear settings cache so new env vars are picked up
        get_settings.cache_clear()

        # Create owner context with NO jira and NO slack bindings
        github_creds = GitHubCredentials(
            kind="app",
            app_id=12345,
            private_key_pem="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
            installation_id=67890,
        )
        config = get_app_config()
        context = OwnerContext(
            id="test-owner",
            is_default=False,
            github=github_creds,
            jira=None,  # No Jira binding
            slack=None,  # No Slack binding
            config=config,
        )

        # Track all HTTP requests
        jira_requests = respx.route(host__regex=r".*jira.*|.*atlassian.*")
        jira_requests.mock(return_value=Response(200, json={}))

        slack_requests = respx.route(host="slack.com")
        slack_requests.mock(return_value=Response(200, json={"ok": True}))

        # Create publisher from context
        publisher = Publisher.from_context(context)

        # Verify that jira and slack are disabled
        assert not publisher.jira.enabled(), "Jira should be disabled when no binding"
        assert not publisher.slack.enabled(), "Slack should be disabled when no binding"

        # Try to call Jira add_comment - should not make any HTTP request
        # (we'll verify via respx that no request was made to Jira hosts)
        # First verify it raises because it's disabled
        from app.adapters.jira import JiraError

        with pytest.raises(JiraError, match="not configured"):
            await publisher.jira.add_comment("TEST-123", "test comment")

        # Verify NO requests were made to Jira
        assert not jira_requests.called, f"Jira requests should not be made, but got: {jira_requests.calls}"

        # Try to call Slack post_message - should return False without making request
        slack_result = await publisher.slack.post_message("test message")
        assert slack_result is False, "Slack should return False when disabled"

        # Verify NO requests were made to Slack
        assert not slack_requests.called, f"Slack requests should not be made, but got: {slack_requests.calls}"

    @pytest.mark.asyncio
    @respx.mock
    async def test_disabled_jira_does_not_use_global_credentials(self, monkeypatch):
        """JiraClient.disabled() should not make requests even with global credentials set."""
        # Set global settings
        monkeypatch.setenv("JIRA_BASE_URL", "https://should-not-be-used.atlassian.net")
        monkeypatch.setenv("JIRA_EMAIL", "global@example.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "global-secret-token")
        get_settings.cache_clear()

        # Track requests
        jira_requests = respx.route(host__regex=r".*atlassian.*|.*jira.*")
        jira_requests.mock(return_value=Response(200, json={}))

        config = get_app_config()
        jira = JiraClient.disabled(config)

        assert not jira.enabled()
        assert jira.base_url == ""
        assert jira.email == ""
        assert jira.token == ""

        # Trying to use it should raise, not make a request
        from app.adapters.jira import JiraError

        with pytest.raises(JiraError):
            await jira.get_issue("TEST-123")

        assert not jira_requests.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_disabled_slack_does_not_use_global_credentials(self, monkeypatch):
        """SlackClient.disabled() should not make requests even with global credentials set."""
        # Set global settings
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-global-should-not-be-used")
        get_settings.cache_clear()

        # Track requests
        slack_requests = respx.route(host="slack.com")
        slack_requests.mock(return_value=Response(200, json={"ok": True}))

        config = get_app_config()
        slack = SlackClient.disabled(config)

        assert not slack.enabled()
        assert slack._token == ""
        assert slack._channel == ""

        # Calling post_message should return False without making a request
        result = await slack.post_message("test")
        assert result is False

        assert not slack_requests.called


class TestGitHubOwnerInstallationId:
    """Tests that owner GitHub clients don't inherit foreign installation IDs."""

    @pytest.mark.asyncio
    async def test_owner_client_does_lookup_despite_global_installation_id(self, monkeypatch):
        """Owner client with installation_id=0 must look up its own installation,
        NOT use the global GITHUB_INSTALLATION_ID.
        """
        # Set global installation ID that should NOT be used
        monkeypatch.setenv("GITHUB_INSTALLATION_ID", "99999")
        monkeypatch.setenv("GITHUB_APP_ID", "11111")
        monkeypatch.setenv("GITHUB_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----")

        # Clear settings cache so new env vars are picked up
        get_settings.cache_clear()

        # Create owner credentials with installation_id=0 (requires lookup)
        creds = GitHubCredentials(
            kind="app",
            app_id=22222,  # Different app ID
            private_key_pem="-----BEGIN RSA PRIVATE KEY-----\nowner-key\n-----END RSA PRIVATE KEY-----",
            installation_id=0,  # Must look up
        )

        client = GitHubAppClient.from_credentials(creds, owner_id="owner-a")

        # Track API calls
        calls = []

        async def mock_request(method, url, **kwargs):
            calls.append((method, url))
            mock = MagicMock()
            mock.json.return_value = {"id": 55555}  # Return a different ID
            return mock

        with patch.object(client, "_jwt", return_value="mock_jwt"):
            with patch.object(client, "_request", side_effect=mock_request):
                result = await client.resolve_installation_id("org", "repo")

        # Verify:
        # 1. The lookup request was made
        assert len(calls) == 1, f"Expected 1 API call but got {len(calls)}"
        assert "installation" in calls[0][1], "Should have made installation lookup request"

        # 2. The result is from lookup, NOT the global setting
        assert result == 55555, f"Expected 55555 from lookup but got {result}"
        assert result != 99999, "Should NOT have used global GITHUB_INSTALLATION_ID"


class TestFromCredentialsIsolation:
    """Tests that from_credentials/from_binding constructors don't read global settings."""

    def test_github_from_credentials_does_not_call_get_settings(self, monkeypatch):
        """GitHubAppClient.from_credentials() should NOT call get_settings()."""

        # Make get_settings raise an error if called
        def raise_on_call():
            raise AssertionError("get_settings() should not be called for owner clients")

        monkeypatch.setattr("app.adapters.github.get_settings", raise_on_call)

        creds = GitHubCredentials(
            kind="app",
            app_id=12345,
            private_key_pem="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
            installation_id=67890,
        )

        # This should NOT raise
        client = GitHubAppClient.from_credentials(creds, owner_id="test")

        assert client.owner_id == "test"
        assert client._credentials == creds
        assert client.settings is None  # Should not have settings

    def test_jira_from_binding_does_not_call_get_settings(self, monkeypatch):
        """JiraClient.from_binding() should NOT call get_settings()."""

        def raise_on_call():
            raise AssertionError("get_settings() should not be called for owner clients")

        monkeypatch.setattr("app.adapters.jira.get_settings", raise_on_call)

        binding = JiraBinding(
            base_url="https://owner.atlassian.net",
            email="owner@example.com",
            api_token="owner-token",
            projects={"PROJ": "In Review"},
        )
        config = get_app_config()

        # This should NOT raise
        client = JiraClient.from_binding(binding, config)

        assert client.base_url == "https://owner.atlassian.net"
        assert client.email == "owner@example.com"
        assert client.token == "owner-token"

    def test_jira_disabled_does_not_call_get_settings(self, monkeypatch):
        """JiraClient.disabled() should NOT call get_settings()."""

        def raise_on_call():
            raise AssertionError("get_settings() should not be called")

        monkeypatch.setattr("app.adapters.jira.get_settings", raise_on_call)

        config = get_app_config()

        # This should NOT raise
        client = JiraClient.disabled(config)

        assert not client.enabled()
        assert client.base_url == ""
        assert client.email == ""
        assert client.token == ""

    def test_slack_from_binding_does_not_call_get_settings(self, monkeypatch):
        """SlackClient.from_binding() should NOT call get_settings()."""

        def raise_on_call():
            raise AssertionError("get_settings() should not be called for owner clients")

        monkeypatch.setattr("app.adapters.slack.get_settings", raise_on_call)

        binding = SlackBinding(
            enabled=True,
            channel="#owner-channel",
            bot_token="xoxb-owner-token",
        )
        config = get_app_config()

        # This should NOT raise
        client = SlackClient.from_binding(binding, config)

        assert client._enabled is True
        assert client._channel == "#owner-channel"
        assert client._token == "xoxb-owner-token"

    def test_slack_disabled_does_not_call_get_settings(self, monkeypatch):
        """SlackClient.disabled() should NOT call get_settings()."""

        def raise_on_call():
            raise AssertionError("get_settings() should not be called")

        monkeypatch.setattr("app.adapters.slack.get_settings", raise_on_call)

        config = get_app_config()

        # This should NOT raise
        client = SlackClient.disabled(config)

        assert not client.enabled()
        assert client._enabled is False
        assert client._channel == ""
        assert client._token == ""


class TestPublisherFromContextIsolation:
    """Tests that Publisher.from_context() builds isolated clients."""

    def test_from_context_does_not_call_get_settings_for_bindings(self, monkeypatch):
        """Publisher.from_context() should use disabled clients that don't read settings."""
        calls = []

        def tracking_get_settings():
            calls.append("get_settings")
            # We need to return something for legacy client initialization
            from app.config import Settings

            return Settings.model_construct(
                github_app_id=0,
                github_private_key="",
                github_installation_id=0,
            )

        monkeypatch.setattr("app.adapters.jira.get_settings", tracking_get_settings)
        monkeypatch.setattr("app.adapters.slack.get_settings", tracking_get_settings)

        github_creds = GitHubCredentials(
            kind="app",
            app_id=12345,
            private_key_pem="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
            installation_id=67890,
        )
        config = get_app_config()
        context = OwnerContext(
            id="test-owner",
            is_default=False,
            github=github_creds,
            jira=None,
            slack=None,
            config=config,
        )

        # This should NOT call get_settings for Jira or Slack
        calls.clear()
        publisher = Publisher.from_context(context)

        # Verify get_settings was NOT called
        assert len(calls) == 0, f"get_settings was called {len(calls)} times but should not have been"

        # Verify clients are properly disabled
        assert not publisher.jira.enabled()
        assert not publisher.slack.enabled()
