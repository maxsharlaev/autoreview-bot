"""Pydantic models for owner configuration YAML."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

OWNER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _validate_env_var_name(value: str | None, field_name: str) -> str | None:
    """Validate that an env var name matches ^[A-Z][A-Z0-9_]*$ and is not a literal secret."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not ENV_VAR_NAME_PATTERN.match(value):
        raise ValueError(
            f"{field_name} must be a valid environment variable name "
            f"(uppercase letters, digits, underscores, starting with a letter)"
        )
    return value


class OwnerGitHubYaml(BaseModel, extra="forbid"):
    """GitHub credentials block for an owner."""

    auth: Literal["app", "pat"] = "app"
    app_id: int | None = None
    app_id_env: str | None = None
    private_key_env: str | None = None
    private_key_file: str | None = None
    installation_id: int = 0
    token_env: str | None = None
    webhook_secret_env: str | None = None
    allowed_repos: list[str] = Field(default_factory=list)

    @field_validator("app_id_env", "private_key_env", "token_env", "webhook_secret_env", mode="after")
    @classmethod
    def validate_env_var_names(cls, value: str | None, info) -> str | None:
        return _validate_env_var_name(value, info.field_name)

    @model_validator(mode="after")
    def validate_auth_fields(self) -> OwnerGitHubYaml:
        if self.auth == "app":
            if self.app_id is None and self.app_id_env is None:
                pass  # Will use convention OWNER_<ID>_GITHUB_APP_ID
            if self.token_env is not None:
                raise ValueError("token_env is only valid with auth=pat")
        elif self.auth == "pat":
            if self.app_id is not None or self.app_id_env is not None:
                raise ValueError("app_id/app_id_env are only valid with auth=app")
            if self.private_key_env is not None or self.private_key_file is not None:
                raise ValueError("private_key_env/private_key_file are only valid with auth=app")
            if self.installation_id != 0:
                raise ValueError("installation_id is only valid with auth=app")
        return self


class OwnerApiYaml(BaseModel, extra="forbid"):
    """API key configuration for an owner."""

    key_env: str | None = None

    @field_validator("key_env", mode="after")
    @classmethod
    def validate_env_var_name(cls, value: str | None, info) -> str | None:
        return _validate_env_var_name(value, info.field_name)


class OwnerJiraProjectYaml(BaseModel, extra="forbid"):
    """Jira project settings for an owner."""

    rework_status: str = "In Progress"


class OwnerJiraYaml(BaseModel, extra="forbid"):
    """Jira binding for an owner."""

    base_url: str = ""
    email: str = ""
    api_token_env: str | None = None
    projects: dict[str, OwnerJiraProjectYaml] = Field(default_factory=dict)

    @field_validator("api_token_env", mode="after")
    @classmethod
    def validate_env_var_name(cls, value: str | None, info) -> str | None:
        return _validate_env_var_name(value, info.field_name)


class OwnerSlackYaml(BaseModel, extra="forbid"):
    """Slack binding for an owner."""

    enabled: bool = True
    channel: str = ""
    bot_token_env: str | None = None

    @field_validator("bot_token_env", mode="after")
    @classmethod
    def validate_env_var_name(cls, value: str | None, info) -> str | None:
        return _validate_env_var_name(value, info.field_name)


class OwnerPublicReposYaml(BaseModel, extra="forbid"):
    """Override public_repos settings for an owner (partial, merged over global)."""

    jira_disclosure: Literal["none", "key_only", "full"] | None = None
    security_findings: Literal["redact", "redact_all", "full"] | None = None


class OwnerLanguageYaml(BaseModel, extra="forbid"):
    """Override language settings for an owner (partial)."""

    summary: Literal["en", "ru"] | None = None
    details: Literal["en", "ru"] | None = None


class OwnerFeaturesYaml(BaseModel, extra="forbid"):
    """Override features settings for an owner (partial)."""

    jira_comment: bool | None = None
    jira_transition: bool | None = None
    digest_enabled: bool | None = None


class OwnerCodexYaml(BaseModel, extra="forbid"):
    """Per-owner model settings; other codex fields (sandbox, limits) stay global."""

    model: str | None = None
    reasoning_effort: str | None = None
    api_key_env: str | None = None

    @field_validator("api_key_env", mode="after")
    @classmethod
    def validate_env_var_name(cls, value: str | None, info) -> str | None:
        return _validate_env_var_name(value, info.field_name)


class OwnerPrDescriptionYaml(BaseModel, extra="forbid"):
    """Override pr_description settings for an owner (partial)."""

    enabled: bool | None = None
    mode: Literal["comment", "fill_empty", "append"] | None = None
    title_mode: Literal["off", "until_human_edit", "when_invalid_or_inconsistent"] | None = None
    check_title_relevance: bool | None = None
    timeout_seconds: int | None = None
    max_commit_messages: int | None = None
    max_commit_chars: int | None = None
    language: str | None = None
    prompt_file: str | None = None


class OwnerSizeLimitYaml(BaseModel, extra="forbid"):
    """Partial size limit: unset fields keep the global value."""

    commits: int | None = None
    changed_lines: int | None = None


class OwnerSizeGuardYaml(BaseModel, extra="forbid"):
    """Override size_guard settings for an owner (partial)."""

    soft: OwnerSizeLimitYaml | None = None
    hard: OwnerSizeLimitYaml | None = None
    override_label: str | None = None


class OwnerYaml(BaseModel, extra="forbid"):
    """Configuration for a single owner in the owners block."""

    default: bool = False
    enabled: bool = True
    aliases: list[str] = Field(default_factory=list)

    github: OwnerGitHubYaml = Field(default_factory=OwnerGitHubYaml)
    api: OwnerApiYaml = Field(default_factory=OwnerApiYaml)
    jira: OwnerJiraYaml | None = None
    slack: OwnerSlackYaml | None = None

    # Partial policy overrides, deep-merged over the global sections (see registry._merge_owner_config)
    public_repos: OwnerPublicReposYaml | None = None
    language: OwnerLanguageYaml | None = None
    features: OwnerFeaturesYaml | None = None
    codex: OwnerCodexYaml | None = None
    pr_description: OwnerPrDescriptionYaml | None = None
    size_guard: OwnerSizeGuardYaml | None = None

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: list[str]) -> list[str]:
        for alias in value:
            if not OWNER_ID_PATTERN.match(alias):
                raise ValueError(f"Invalid alias '{alias}': must match {OWNER_ID_PATTERN.pattern}")
        return value

    def has_overrides(self) -> bool:
        """Return True if any override fields are set."""
        return any(
            [
                self.public_repos is not None,
                self.language is not None,
                self.features is not None,
                self.codex is not None,
                self.pr_description is not None,
                self.size_guard is not None,
            ]
        )


def validate_owner_id(owner_id: str) -> None:
    """Validate that an owner_id matches the required pattern."""
    if not OWNER_ID_PATTERN.match(owner_id):
        raise ValueError(f"Invalid owner_id '{owner_id}': must match {OWNER_ID_PATTERN.pattern}")
