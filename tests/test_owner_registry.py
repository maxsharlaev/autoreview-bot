"""Tests for multi-owner registry: modes A-D, validation, and backward compatibility."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from app.config import AppConfig, GitHubYaml, JiraProjectYaml, JiraYaml, SlackYaml, load_yaml_config
from app.owners.context import GitHubCredentials, JiraBinding, SlackBinding
from app.owners.registry import OwnerConfigError, OwnerRegistry, _env_name_for_owner
from app.owners.schema import OwnerGitHubYaml, OwnerYaml, validate_owner_id
from pydantic import ValidationError

# --- Schema validation tests ---


def test_owner_id_validation_valid() -> None:
    """Valid owner IDs should pass validation."""
    for owner_id in ["default", "org-a", "org_b", "a123", "x" * 64]:
        validate_owner_id(owner_id)


def test_owner_id_validation_invalid() -> None:
    """Invalid owner IDs should raise ValueError."""
    for owner_id in ["", "-start", "_start", "UPPER", "with space", "x" * 65, "a/b"]:
        with pytest.raises(ValueError):
            validate_owner_id(owner_id)


def test_owner_yaml_forbids_extra_fields() -> None:
    """OwnerYaml should reject unknown fields (extra=forbid)."""
    with pytest.raises(ValidationError):
        OwnerYaml.model_validate({"unknown_field": True})


def test_owner_github_yaml_pat_rejects_app_fields() -> None:
    """PAT auth should reject app_id/private_key_env/installation_id."""
    with pytest.raises(ValidationError):
        OwnerGitHubYaml(auth="pat", app_id=123)

    with pytest.raises(ValidationError):
        OwnerGitHubYaml(auth="pat", private_key_env="X")

    with pytest.raises(ValidationError):
        OwnerGitHubYaml(auth="pat", installation_id=456)


def test_owner_github_yaml_app_rejects_token_env() -> None:
    """App auth should reject token_env."""
    with pytest.raises(ValidationError):
        OwnerGitHubYaml(auth="app", token_env="X")


# --- Env name convention tests ---


def test_env_name_for_owner() -> None:
    """Test OWNER_<ID>_<NAME> convention."""
    assert _env_name_for_owner("org-a", "GITHUB_TOKEN") == "OWNER_ORG_A_GITHUB_TOKEN"
    assert _env_name_for_owner("my_owner", "JIRA_API_TOKEN") == "OWNER_MY_OWNER_JIRA_API_TOKEN"
    assert _env_name_for_owner("default", "REVIEW_API_KEY") == "OWNER_DEFAULT_REVIEW_API_KEY"


# --- Settings mock helper ---


def _mock_settings(
    github_token: str = "",
    github_app_id: int = 0,
    github_app_private_key: str = "",
    github_installation_id: int = 0,
    github_webhook_secret: str = "",
    jira_base_url: str = "",
    jira_email: str = "",
    jira_api_token: str = "",
    slack_bot_token: str = "",
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
    mock.jira_base_url = jira_base_url
    mock.jira_email = jira_email
    mock.jira_api_token = jira_api_token
    mock.slack_bot_token = slack_bot_token
    mock.openai_api_key = openai_api_key
    mock.review_api_key = review_api_key
    mock.github_private_key_pem.return_value = github_app_private_key
    return mock


# --- Mode A: Legacy env-only tests ---


def test_mode_a_legacy_pat() -> None:
    """Mode A: Legacy PAT credentials create a default owner."""
    settings = _mock_settings(github_token="ghp_test123")
    config = AppConfig()

    registry = OwnerRegistry.build(settings, config, env={})

    assert len(registry.owners) == 1
    assert "default" in registry.owners
    assert registry.default_owner_id == "default"

    ctx = registry.get_default()
    assert ctx.id == "default"
    assert ctx.github.kind == "pat"
    assert ctx.github.token == "ghp_test123"


def test_mode_a_legacy_app() -> None:
    """Mode A: Legacy App credentials create a default owner."""
    settings = _mock_settings(
        github_app_id=12345,
        github_app_private_key="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
        github_installation_id=67890,
    )
    config = AppConfig()

    registry = OwnerRegistry.build(settings, config, env={})

    assert len(registry.owners) == 1
    ctx = registry.get_default()
    assert ctx.github.kind == "app"
    assert ctx.github.app_id == 12345
    assert ctx.github.installation_id == 67890


def test_mode_a_no_github_creds_fails() -> None:
    """Mode A: No GitHub credentials at all should fail."""
    settings = _mock_settings()
    config = AppConfig()

    with pytest.raises(OwnerConfigError, match="No GitHub credentials configured"):
        OwnerRegistry.build(settings, config, env={})


# --- Mode B: Legacy + extra owners tests ---


def test_mode_b_legacy_plus_owners() -> None:
    """Mode B: Legacy creds + owners block creates multiple owners."""
    settings = _mock_settings(github_token="ghp_legacy")
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat"},
            }
        }
    )
    env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a"}

    registry = OwnerRegistry.build(settings, config, env=env)

    assert len(registry.owners) == 2
    assert "default" in registry.owners
    assert "org-a" in registry.owners
    assert registry.default_owner_id == "default"


def test_mode_b_default_owner_id_conflict() -> None:
    """Mode B: Owner named 'default' in owners block should fail."""
    settings = _mock_settings(github_token="ghp_legacy")
    config = AppConfig(
        owners={
            "default": {
                "github": {"auth": "pat"},
            }
        }
    )
    env = {"OWNER_DEFAULT_GITHUB_TOKEN": "ghp_test"}

    with pytest.raises(OwnerConfigError, match="Cannot have owner 'default'.*mode B conflict"):
        OwnerRegistry.build(settings, config, env=env)


def test_mode_b_named_owner_becomes_default() -> None:
    """Mode B: Named owner with default=true becomes the default fallback."""
    settings = _mock_settings(github_token="ghp_legacy")
    config = AppConfig(
        owners={
            "org-a": {
                "default": True,
                "github": {"auth": "pat"},
            }
        }
    )
    env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a"}

    registry = OwnerRegistry.build(settings, config, env=env)

    assert registry.default_owner_id == "org-a"
    # Legacy owner still exists
    assert "default" in registry.owners


# --- Mode C: Single owner block tests ---


def test_mode_c_single_owner() -> None:
    """Mode C: Single owner in owners block, no legacy creds."""
    settings = _mock_settings()  # No legacy creds
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat"},
            }
        }
    )
    env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a"}

    registry = OwnerRegistry.build(settings, config, env=env)

    assert len(registry.owners) == 1
    assert "org-a" in registry.owners
    assert registry.default_owner_id == "org-a"
    assert "default" not in registry.owners


# --- Mode D: Multiple owners tests ---


def test_mode_d_multiple_owners_with_default_flag() -> None:
    """Mode D: Multiple owners with explicit default=true."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat"},
            },
            "org-b": {
                "default": True,
                "github": {"auth": "pat"},
            },
        }
    )
    env = {
        "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
        "OWNER_ORG_B_GITHUB_TOKEN": "ghp_org_b",
    }

    registry = OwnerRegistry.build(settings, config, env=env)

    assert len(registry.owners) == 2
    assert registry.default_owner_id == "org-b"


def test_mode_d_multiple_owners_without_default_flag_warns() -> None:
    """Mode D: Multiple owners without default=true uses first and warns."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat"},
            },
            "org-b": {
                "github": {"auth": "pat"},
            },
        }
    )
    env = {
        "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
        "OWNER_ORG_B_GITHUB_TOKEN": "ghp_org_b",
    }

    registry = OwnerRegistry.build(settings, config, env=env)

    assert registry.default_owner_id == "org-a"
    assert any("default=true" in w for w in registry.warnings)


def test_mode_d_multiple_defaults_fails() -> None:
    """Mode D: Multiple owners with default=true should fail."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "default": True,
                "github": {"auth": "pat"},
            },
            "org-b": {
                "default": True,
                "github": {"auth": "pat"},
            },
        }
    )
    env = {
        "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
        "OWNER_ORG_B_GITHUB_TOKEN": "ghp_org_b",
    }

    with pytest.raises(OwnerConfigError, match="Multiple owners have default=true"):
        OwnerRegistry.build(settings, config, env=env)


# --- Startup validation failure tests ---


def test_validation_same_repo_claimed_by_two_owners() -> None:
    """Two owners claiming the same exact repo should fail."""
    settings = _mock_settings()
    config = AppConfig(
        github=GitHubYaml(allowed_repos=["org/repo"]),
        owners={
            "org-a": {
                "github": {"auth": "pat", "allowed_repos": ["org/repo"]},
            },
        },
    )
    env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a"}

    # Need legacy creds to have both owners
    settings.github_token = "ghp_legacy"

    with pytest.raises(OwnerConfigError, match="claimed by both"):
        OwnerRegistry.build(settings, config, env=env)


def test_validation_same_wildcard_claimed_by_two_owners() -> None:
    """Two owners claiming the same wildcard should fail."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat", "allowed_repos": ["org/*"]},
            },
            "org-b": {
                "github": {"auth": "pat", "allowed_repos": ["org/*"]},
            },
        },
    )
    env = {
        "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
        "OWNER_ORG_B_GITHUB_TOKEN": "ghp_org_b",
    }

    with pytest.raises(OwnerConfigError, match="claimed by both"):
        OwnerRegistry.build(settings, config, env=env)


def test_validation_same_app_installation_id() -> None:
    """Two owners with same (app_id, installation_id) should fail."""
    settings = _mock_settings()
    key = "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "app", "app_id": 123, "installation_id": 456},
            },
            "org-b": {
                "github": {"auth": "app", "app_id": 123, "installation_id": 456},
            },
        },
    )
    env = {
        "OWNER_ORG_A_GITHUB_APP_PRIVATE_KEY": key,
        "OWNER_ORG_B_GITHUB_APP_PRIVATE_KEY": key,
    }

    with pytest.raises(OwnerConfigError, match="Same.*app_id.*installation_id"):
        OwnerRegistry.build(settings, config, env=env)


def test_validation_same_api_key_fails() -> None:
    """Two owners with the same API key should fail."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat"},
                "api": {"key_env": "SHARED_KEY"},
            },
            "org-b": {
                "github": {"auth": "pat"},
                "api": {"key_env": "SHARED_KEY"},
            },
        },
    )
    env = {
        "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
        "OWNER_ORG_B_GITHUB_TOKEN": "ghp_org_b",
        "SHARED_KEY": "api_key_value",
    }

    with pytest.raises(OwnerConfigError, match="API key is used by both"):
        OwnerRegistry.build(settings, config, env=env)


def test_validation_same_pat_warns() -> None:
    """Two owners with the same PAT should warn but not fail."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat", "token_env": "SHARED_TOKEN"},
            },
            "org-b": {
                "github": {"auth": "pat", "token_env": "SHARED_TOKEN"},
            },
        },
    )
    env = {"SHARED_TOKEN": "ghp_shared"}

    registry = OwnerRegistry.build(settings, config, env=env)

    assert any("Same GitHub PAT" in w for w in registry.warnings)


def test_validation_empty_allowed_repos_warns() -> None:
    """Empty allowed_repos should warn."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat", "allowed_repos": []},
            },
        },
    )
    env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a"}

    registry = OwnerRegistry.build(settings, config, env=env)

    assert any("empty allowed_repos" in w for w in registry.warnings)


def test_validation_missing_required_env_fails() -> None:
    """Missing required env variable should fail with var name but not value."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat"},
            },
        },
    )
    env = {}  # Missing OWNER_ORG_A_GITHUB_TOKEN

    with pytest.raises(OwnerConfigError) as exc_info:
        OwnerRegistry.build(settings, config, env=env)

    # Should mention the env var name
    assert "OWNER_ORG_A_GITHUB_TOKEN" in str(exc_info.value)
    # Should NOT contain any actual secrets (but we have none here anyway)


def test_validation_exact_repo_overrides_wildcard_logs() -> None:
    """Exact repo claim vs wildcard should log info about precedence."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat", "allowed_repos": ["org/specific-repo"]},
            },
            "org-b": {
                "github": {"auth": "pat", "allowed_repos": ["org/*"]},
            },
        },
    )
    env = {
        "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
        "OWNER_ORG_B_GITHUB_TOKEN": "ghp_org_b",
    }

    registry = OwnerRegistry.build(settings, config, env=env)

    assert any("exact claim takes precedence" in w for w in registry.warnings)


# --- Alias tests ---


def test_aliases_resolve() -> None:
    """Aliases should resolve to the correct owner."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-new": {
                "aliases": ["org-old", "legacy"],
                "github": {"auth": "pat"},
            },
        },
    )
    env = {"OWNER_ORG_NEW_GITHUB_TOKEN": "ghp_org_new"}

    registry = OwnerRegistry.build(settings, config, env=env)

    assert registry.get("org-new") is not None
    assert registry.get("org-old") is not None
    assert registry.get("legacy") is not None
    assert registry.get("org-new") == registry.get("org-old")


def test_alias_conflicts_with_owner_id_fails() -> None:
    """Alias matching an owner ID should fail."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "aliases": ["org-b"],
                "github": {"auth": "pat"},
            },
            "org-b": {
                "github": {"auth": "pat"},
            },
        },
    )
    env = {
        "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
        "OWNER_ORG_B_GITHUB_TOKEN": "ghp_org_b",
    }

    with pytest.raises(OwnerConfigError, match="conflicts with owner id"):
        OwnerRegistry.build(settings, config, env=env)


# --- Legacy config equivalence test ---


def test_legacy_config_equivalence() -> None:
    """Legacy owner's effective config should match get_app_config() logic."""
    settings = _mock_settings(
        github_token="ghp_test",
        github_app_id=123,
        github_installation_id=456,
        github_webhook_secret="secret1234567890",
        jira_base_url="https://test.atlassian.net",
        jira_email="test@example.com",
        jira_api_token="jira_token",
        slack_bot_token="xoxb-slack",
        openai_api_key="sk-openai",
        review_api_key="api_key_123",
    )

    # Simulate YAML config with some values
    config = AppConfig(
        github=GitHubYaml(
            app_id=789,  # Should be overridden by settings
            installation_id=101112,  # Should be overridden by settings
            allowed_repos=["org/repo1", "org/repo2"],
        ),
        jira=JiraYaml(
            base_url="https://yaml.atlassian.net",  # Should be overridden by settings
            email="yaml@example.com",  # Should be overridden by settings
            projects={"ABC": JiraProjectYaml(rework_status="In Review")},
        ),
        slack=SlackYaml(enabled=True, channel="#review"),
    )

    registry = OwnerRegistry.build(settings, config, env={})
    ctx = registry.get_default()

    # PAT takes precedence over App
    assert ctx.github.kind == "pat"
    assert ctx.github.token == "ghp_test"

    # Webhook secret from settings
    assert ctx.webhook_secret == "secret1234567890"

    # Jira from settings overriding YAML
    assert ctx.jira is not None
    assert ctx.jira.base_url == "https://test.atlassian.net"
    assert ctx.jira.email == "test@example.com"
    assert ctx.jira.api_token == "jira_token"
    assert ctx.jira.projects == {"ABC": "In Review"}

    # Slack from YAML + settings
    assert ctx.slack is not None
    assert ctx.slack.enabled is True
    assert ctx.slack.channel == "#review"
    assert ctx.slack.bot_token == "xoxb-slack"

    # Other secrets
    assert ctx.api_key == "api_key_123"
    assert ctx.openai_api_key == "sk-openai"


def test_legacy_config_with_example_yaml() -> None:
    """Config from config.example.yaml should work with legacy env."""
    config = load_yaml_config(Path("config.example.yaml"))
    settings = _mock_settings(
        github_token="ghp_test",
        github_webhook_secret="secret1234567890",
    )

    registry = OwnerRegistry.build(settings, config, env={})

    assert "default" in registry.owners
    ctx = registry.get_default()
    assert ctx.config.github.allowed_repos == ["example-org/example-repo"]


# --- Disabled owner tests ---


def test_disabled_owner_not_included() -> None:
    """Disabled owners should not be in the registry."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "enabled": False,
                "github": {"auth": "pat"},
            },
            "org-b": {
                "github": {"auth": "pat"},
            },
        },
    )
    env = {
        "OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a",
        "OWNER_ORG_B_GITHUB_TOKEN": "ghp_org_b",
    }

    registry = OwnerRegistry.build(settings, config, env=env)

    assert "org-a" not in registry.owners
    assert "org-b" in registry.owners


# --- Jira/Slack binding tests ---


def test_owner_without_jira_has_none_jira() -> None:
    """Owner without jira block has jira=None."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat"},
                # No jira block
            },
        },
    )
    env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a"}

    registry = OwnerRegistry.build(settings, config, env=env)

    ctx = registry.get("org-a")
    assert ctx is not None
    assert ctx.jira is None


def test_owner_without_slack_has_none_slack() -> None:
    """Owner without slack block has slack=None."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat"},
                # No slack block
            },
        },
    )
    env = {"OWNER_ORG_A_GITHUB_TOKEN": "ghp_org_a"}

    registry = OwnerRegistry.build(settings, config, env=env)

    ctx = registry.get("org-a")
    assert ctx is not None
    assert ctx.slack is None


# --- Context dataclass tests ---


def test_github_credentials_pat() -> None:
    """PAT credentials should validate correctly."""
    creds = GitHubCredentials(kind="pat", token="ghp_test")
    assert creds.kind == "pat"
    assert creds.token == "ghp_test"


def test_github_credentials_pat_missing_token_fails() -> None:
    """PAT credentials without token should fail."""
    with pytest.raises(ValueError, match="PAT credentials require a token"):
        GitHubCredentials(kind="pat", token=None)


def test_github_credentials_app() -> None:
    """App credentials should validate correctly."""
    creds = GitHubCredentials(kind="app", app_id=123, private_key_pem="-----BEGIN RSA PRIVATE KEY-----")
    assert creds.kind == "app"
    assert creds.app_id == 123


def test_github_credentials_app_missing_id_fails() -> None:
    """App credentials without app_id should fail."""
    with pytest.raises(ValueError, match="App credentials require app_id"):
        GitHubCredentials(kind="app", app_id=None, private_key_pem="-----BEGIN RSA PRIVATE KEY-----")


def test_jira_binding_enabled() -> None:
    """JiraBinding.enabled() should work correctly."""
    binding = JiraBinding(base_url="https://x.atlassian.net", email="x@y.com", api_token="token", projects={})
    assert binding.enabled() is True

    empty = JiraBinding(base_url="", email="", api_token="", projects={})
    assert empty.enabled() is False


def test_slack_binding_is_enabled() -> None:
    """SlackBinding.is_enabled() should work correctly."""
    binding = SlackBinding(enabled=True, channel="#test", bot_token="xoxb-test")
    assert binding.is_enabled() is True

    disabled = SlackBinding(enabled=False, channel="#test", bot_token="xoxb-test")
    assert disabled.is_enabled() is False

    no_token = SlackBinding(enabled=True, channel="#test", bot_token="")
    assert no_token.is_enabled() is False


# --- Error message safety tests ---


def test_error_messages_do_not_contain_secrets() -> None:
    """Error messages should contain env var names but never actual secret values."""
    settings = _mock_settings()
    config = AppConfig(
        owners={
            "org-a": {
                "github": {"auth": "pat", "token_env": "MY_SECRET_TOKEN"},
            },
        },
    )
    env = {}  # Missing

    with pytest.raises(OwnerConfigError) as exc_info:
        OwnerRegistry.build(settings, config, env=env)

    error_msg = str(exc_info.value)
    assert "MY_SECRET_TOKEN" in error_msg  # Var name is OK
    # Can't really test for absence of secret values since we don't have any,
    # but the code structure ensures we only report var names
