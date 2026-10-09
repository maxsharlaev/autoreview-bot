"""Application settings: env secrets plus versioned YAML."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class GitHubYaml(BaseModel):
    app_id: int = 0
    installation_id: int = 0
    allowed_repos: list[str] = Field(default_factory=list)


class JiraProjectYaml(BaseModel):
    rework_status: str = "In Progress"


class JiraYaml(BaseModel):
    base_url: str = ""
    email: str = ""
    projects: dict[str, JiraProjectYaml] = Field(default_factory=dict)


class SlackYaml(BaseModel):
    enabled: bool = False
    channel: str = ""


class DigestScheduleYaml(BaseModel):
    enabled: bool = True
    cron: str = "0 * * * *"


class ScheduleYaml(BaseModel):
    review_digest: DigestScheduleYaml = Field(default_factory=DigestScheduleYaml)


class FeaturesYaml(BaseModel):
    jira_comment: bool = True
    jira_transition: bool = True
    digest_enabled: bool = True


class LanguageYaml(BaseModel):
    summary: Literal["en", "ru"] = "en"
    details: Literal["en", "ru"] = "en"


class PrDescriptionYaml(BaseModel):
    enabled: bool = False
    mode: Literal["comment", "fill_empty", "append"] = "comment"
    title_mode: Literal["off", "until_human_edit", "when_invalid_or_inconsistent"] = "off"
    check_title_relevance: bool = True
    timeout_seconds: int = Field(default=180, gt=0, le=600)
    max_commit_messages: int = Field(default=250, gt=0, le=250)  # legacy count cap
    max_commit_chars: int = Field(default=12000, gt=0, le=50000)
    language: str | None = None
    prompt_file: str = ""

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str | None) -> str | None:
        return PrTextYaml.validate_language(value)

    @field_validator("title_mode", mode="before")
    @classmethod
    def normalize_title_mode(cls, value: object) -> object:
        if value is False:
            return "off"
        if value == "always":
            logger.warning("pr_description.title_mode=always is deprecated; use until_human_edit")
            return "until_human_edit"
        return value


class PrTextYaml(BaseModel):
    language: str | None = None

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = value.strip().lower()
        if code != "auto" and not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", code):
            raise ValueError("language must be 'auto' or a BCP 47 language code such as en, ru, fr, or pt-BR")
        return code


class SizeLimitYaml(BaseModel):
    commits: int = Field(gt=0)
    changed_lines: int = Field(gt=0)


class SizeGuardYaml(BaseModel):
    soft: SizeLimitYaml = Field(default_factory=lambda: SizeLimitYaml(commits=50, changed_lines=5000))
    hard: SizeLimitYaml = Field(default_factory=lambda: SizeLimitYaml(commits=150, changed_lines=20000))
    override_label: str = "autoreview:force"

    @model_validator(mode="after")
    def validate_thresholds(self) -> SizeGuardYaml:
        if self.hard.commits < self.soft.commits or self.hard.changed_lines < self.soft.changed_lines:
            raise ValueError("size_guard.hard thresholds must be at least size_guard.soft thresholds")
        if not self.override_label.strip():
            raise ValueError("size_guard.override_label must not be empty")
        return self


class PublicReposYaml(BaseModel):
    jira_disclosure: Literal["none", "key_only", "full"] = "key_only"
    security_findings: Literal["redact", "redact_all", "full"] = "redact"


class CodexYaml(BaseModel):
    model: str = "gpt-5.6-sol"
    prompt_file: str = ""
    reasoning_effort: str = "medium"
    sandbox: str = "read-only"
    approval_policy: str = "never"
    timeout_seconds: int = 600
    max_files: int = 100
    max_diff_lines: int = 20000
    max_findings: int = 20
    max_file_bytes: int = 524288


class RoutingYaml(BaseModel, extra="forbid"):
    """Routing configuration for unclaimed repos."""

    unclaimed: Literal["default", "reject"] = "default"


class AppConfig(BaseModel):
    github: GitHubYaml = Field(default_factory=GitHubYaml)
    jira: JiraYaml = Field(default_factory=JiraYaml)
    slack: SlackYaml = Field(default_factory=SlackYaml)
    schedule: ScheduleYaml = Field(default_factory=ScheduleYaml)
    features: FeaturesYaml = Field(default_factory=FeaturesYaml)
    language: LanguageYaml = Field(default_factory=LanguageYaml)
    pr_description: PrDescriptionYaml = Field(default_factory=PrDescriptionYaml)
    pr_text: PrTextYaml = Field(default_factory=PrTextYaml)
    size_guard: SizeGuardYaml = Field(default_factory=SizeGuardYaml)
    public_repos: PublicReposYaml = Field(default_factory=PublicReposYaml)
    codex: CodexYaml = Field(default_factory=CodexYaml)

    # Multi-owner support (M1): owners block and routing config
    # Raw dicts are validated in app.owners.registry.OwnerRegistry.build()
    # The YAML ignores unknown keys (model default), allowing old code to read new configs
    owners: dict = Field(default_factory=dict)
    routing: RoutingYaml = Field(default_factory=RoutingYaml)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = "postgresql+asyncpg://review:review@localhost:5432/review"
    redis_url: str = "redis://localhost:6379/0"
    github_app_id: int = 0
    github_app_private_key: str = ""
    github_webhook_secret: str = ""
    github_installation_id: int = 0
    github_token: str = Field(default="", validation_alias=AliasChoices("GITHUB_TOKEN", "GITHUB_PAT"))
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    slack_bot_token: str = ""
    openai_api_key: str = ""
    review_api_key: str = ""
    worker_max_jobs: int = 4
    worker_job_timeout: int = 900
    config_path: str = "config.yaml"
    log_level: str = "INFO"
    log_format: str = "text"
    metrics_port: int = 9100
    run_migrations: bool = False

    def github_private_key_pem(self) -> str:
        key = self.github_app_private_key.replace("\\n", "\n").strip()
        if key.startswith("-----"):
            return key
        path = Path(key)
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return key


def load_yaml_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.is_file():
        return AppConfig()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_app_config() -> AppConfig:
    settings = get_settings()
    config = load_yaml_config(settings.config_path)
    github = config.github.model_copy()
    if settings.github_app_id:
        github.app_id = settings.github_app_id
    if settings.github_installation_id:
        github.installation_id = settings.github_installation_id
    jira = config.jira.model_copy()
    if settings.jira_base_url:
        jira.base_url = settings.jira_base_url.rstrip("/")
    if settings.jira_email:
        jira.email = settings.jira_email
    return config.model_copy(update={"github": github, "jira": jira})


def repo_allowed(full_name: str, config: AppConfig | None = None) -> bool:
    cfg = config or get_app_config()
    allow = [item.lower() for item in cfg.github.allowed_repos]
    if not allow:
        return True
    return full_name.lower() in allow
