"""Owner context: frozen credentials and bindings for a single owner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.config import AppConfig


@dataclass(frozen=True)
class GitHubCredentials:
    """GitHub authentication credentials for an owner."""

    kind: Literal["app", "pat"]
    token: str | None = None
    app_id: int | None = None
    private_key_pem: str | None = None
    installation_id: int = 0

    def __post_init__(self) -> None:
        if self.kind == "pat" and not self.token:
            raise ValueError("PAT credentials require a token")
        if self.kind == "app" and (not self.app_id or not self.private_key_pem):
            raise ValueError("App credentials require app_id and private_key_pem")


@dataclass(frozen=True)
class JiraBinding:
    """Jira credentials and configuration for an owner."""

    base_url: str
    email: str
    api_token: str
    projects: dict[str, str]  # project key -> rework_status

    def enabled(self) -> bool:
        return bool(self.base_url and self.email and self.api_token)


@dataclass(frozen=True)
class SlackBinding:
    """Slack credentials and configuration for an owner."""

    enabled: bool
    channel: str
    bot_token: str

    def is_enabled(self) -> bool:
        return bool(self.enabled and self.bot_token and self.channel)


@dataclass(frozen=True)
class OwnerContext:
    """Frozen context for a single owner: credentials, bindings, and effective config."""

    id: str
    is_default: bool
    github: GitHubCredentials
    webhook_secret: str | None
    api_key: str | None
    jira: JiraBinding | None
    slack: SlackBinding | None
    openai_api_key: str | None
    config: AppConfig

    def webhook_enabled(self) -> bool:
        """Return True if this owner has a valid webhook secret."""
        return bool(self.webhook_secret and len(self.webhook_secret.strip()) >= 16)
