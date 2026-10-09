"""Pydantic models for owner configuration YAML."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

OWNER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


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


class OwnerJiraProjectYaml(BaseModel, extra="forbid"):
    """Jira project settings for an owner."""

    rework_status: str = "In Progress"


class OwnerJiraYaml(BaseModel, extra="forbid"):
    """Jira binding for an owner."""

    base_url: str = ""
    email: str = ""
    api_token_env: str | None = None
    projects: dict[str, OwnerJiraProjectYaml] = Field(default_factory=dict)


class OwnerSlackYaml(BaseModel, extra="forbid"):
    """Slack binding for an owner."""

    enabled: bool = True
    channel: str = ""
    bot_token_env: str | None = None


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
    """Override codex settings for an owner (M3: model, reasoning_effort, api_key_env)."""

    model: str | None = None
    reasoning_effort: str | None = None
    api_key_env: str | None = None


class OwnerYaml(BaseModel, extra="forbid"):
    """Configuration for a single owner in the owners block."""

    default: bool = False
    enabled: bool = True
    aliases: list[str] = Field(default_factory=list)

    github: OwnerGitHubYaml = Field(default_factory=OwnerGitHubYaml)
    api: OwnerApiYaml = Field(default_factory=OwnerApiYaml)
    jira: OwnerJiraYaml | None = None
    slack: OwnerSlackYaml | None = None

    public_repos: OwnerPublicReposYaml | None = None
    language: OwnerLanguageYaml | None = None
    features: OwnerFeaturesYaml | None = None
    codex: OwnerCodexYaml | None = None

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: list[str]) -> list[str]:
        for alias in value:
            if not OWNER_ID_PATTERN.match(alias):
                raise ValueError(f"Invalid alias '{alias}': must match {OWNER_ID_PATTERN.pattern}")
        return value


def validate_owner_id(owner_id: str) -> None:
    """Validate that an owner_id matches the required pattern."""
    if not OWNER_ID_PATTERN.match(owner_id):
        raise ValueError(f"Invalid owner_id '{owner_id}': must match {OWNER_ID_PATTERN.pattern}")
