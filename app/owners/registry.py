"""Owner registry: builds and manages owner contexts with startup validation."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from app.config import RoutingYaml
from app.owners.context import GitHubCredentials, JiraBinding, OwnerContext, SlackBinding
from app.owners.schema import OwnerYaml, validate_owner_id

if TYPE_CHECKING:
    from app.config import AppConfig, Settings

logger = logging.getLogger(__name__)

DEFAULT_OWNER_ID = "default"
ENV_VAR_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class OwnerConfigError(ValueError):
    """Raised when owner configuration is invalid at startup."""


@dataclass
class OwnerRegistry:
    """Registry of all owners with their contexts."""

    owners: dict[str, OwnerContext]
    aliases: dict[str, str]  # alias -> owner_id
    default_owner_id: str
    routing: RoutingYaml
    warnings: list[str]

    def get(self, owner_id: str) -> OwnerContext | None:
        """Get owner by id or alias."""
        resolved = self.aliases.get(owner_id, owner_id)
        return self.owners.get(resolved)

    def get_default(self) -> OwnerContext:
        """Get the default owner context."""
        ctx = self.owners.get(self.default_owner_id)
        if ctx is None:
            raise OwnerConfigError("No default owner configured")
        return ctx

    def all_owners(self) -> list[OwnerContext]:
        """Return all enabled owner contexts."""
        return [ctx for ctx in self.owners.values() if ctx.id in self.owners]

    def webhook_secrets(self) -> dict[str, str]:
        """Return mapping of owner_id -> webhook_secret for owners with valid secrets."""
        return {owner_id: ctx.webhook_secret for owner_id, ctx in self.owners.items() if ctx.webhook_enabled()}

    @classmethod
    def build(
        cls,
        settings: Settings,
        app_config: AppConfig,
        env: dict[str, str] | None = None,
    ) -> OwnerRegistry:
        """Build the owner registry from settings and config with full validation.

        Args:
            settings: Application settings (secrets from env/pydantic-settings)
            app_config: Parsed YAML config (includes owners and routing blocks)
            env: Environment variables (os.environ if None; .env as fallback for dynamic names)

        Returns:
            OwnerRegistry with all owners built and validated

        Raises:
            OwnerConfigError: If configuration is invalid (with user-facing message)
        """
        if env is None:
            env = dict(os.environ)

        dotenv_fallback = dotenv_values(".env") if Path(".env").exists() else {}
        raw_owners = getattr(app_config, "owners", None) or {}
        routing = getattr(app_config, "routing", None) or RoutingYaml()

        owners_yaml: dict[str, OwnerYaml] = {}
        for owner_id, raw_config in raw_owners.items():
            try:
                validate_owner_id(owner_id)
                if isinstance(raw_config, dict):
                    owners_yaml[owner_id] = OwnerYaml.model_validate(raw_config)
                elif isinstance(raw_config, OwnerYaml):
                    owners_yaml[owner_id] = raw_config
                else:
                    raise OwnerConfigError(f"Invalid owner config type for '{owner_id}'")
            except Exception as e:
                raise OwnerConfigError(f"Invalid configuration for owner '{owner_id}': {e}") from e

        has_legacy_github = _has_legacy_github_creds(settings)
        has_owners_block = bool(owners_yaml)

        owners: dict[str, OwnerContext] = {}
        aliases: dict[str, str] = {}
        warnings: list[str] = []
        default_owner_id: str | None = None

        if has_legacy_github:
            legacy_ctx = _build_legacy_owner(settings, app_config, env, dotenv_fallback)
            owners[DEFAULT_OWNER_ID] = legacy_ctx
            default_owner_id = DEFAULT_OWNER_ID

        if has_owners_block:
            if has_legacy_github and DEFAULT_OWNER_ID in owners_yaml:
                raise OwnerConfigError(
                    f"Cannot have owner '{DEFAULT_OWNER_ID}' in owners block when legacy GitHub credentials are set "
                    "(mode B conflict). Either remove legacy env vars or rename the owner."
                )

            explicit_defaults = [oid for oid, cfg in owners_yaml.items() if cfg.default]
            if len(explicit_defaults) > 1:
                raise OwnerConfigError(
                    f"Multiple owners have default=true: {', '.join(explicit_defaults)}. "
                    "Only one owner can be the default."
                )

            for owner_id, owner_yaml in owners_yaml.items():
                validate_owner_id(owner_id)

                for alias in owner_yaml.aliases:
                    if alias in aliases:
                        raise OwnerConfigError(f"Alias '{alias}' is used by multiple owners")
                    if alias in owners_yaml:
                        raise OwnerConfigError(f"Alias '{alias}' conflicts with owner id")
                    aliases[alias] = owner_id

                if not owner_yaml.enabled:
                    continue

                ctx = _build_owner_context(
                    owner_id=owner_id,
                    owner_yaml=owner_yaml,
                    app_config=app_config,
                    env=env,
                    dotenv_fallback=dotenv_fallback,
                    settings=settings,
                    warnings=warnings,
                )
                owners[owner_id] = ctx

                if owner_yaml.default:
                    if has_legacy_github:
                        default_owner_id = owner_id
                    else:
                        default_owner_id = owner_id

            if not has_legacy_github and default_owner_id is None:
                if len(owners) == 1:
                    default_owner_id = next(iter(owners.keys()))
                elif owners:
                    first_id = next(iter(owners_yaml.keys()))
                    if first_id in owners:
                        default_owner_id = first_id
                        warnings.append(
                            f"No owner has default=true; using first owner '{first_id}' as default. "
                            "Set default=true explicitly to silence this warning."
                        )

        elif not has_legacy_github:
            raise OwnerConfigError(
                "No GitHub credentials configured. Set GITHUB_TOKEN/GITHUB_PAT or "
                "GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY, or add an owners block to config."
            )

        if default_owner_id is None and owners:
            default_owner_id = next(iter(owners.keys()))

        if default_owner_id is None:
            raise OwnerConfigError("No owners configured")

        # Build allowed_repos map for validation
        # For legacy owner, use app_config.github.allowed_repos
        # For named owners, use owner_yaml.github.allowed_repos
        allowed_repos_map: dict[str, list[str]] = {}
        if DEFAULT_OWNER_ID in owners:
            allowed_repos_map[DEFAULT_OWNER_ID] = app_config.github.allowed_repos
        for owner_id, owner_yaml in owners_yaml.items():
            if owner_id in owners:
                allowed_repos_map[owner_id] = owner_yaml.github.allowed_repos

        _validate_claims(allowed_repos_map, warnings)
        _validate_api_keys(owners)
        _validate_credentials_uniqueness(owners, warnings)

        return cls(
            owners=owners,
            aliases=aliases,
            default_owner_id=default_owner_id,
            routing=routing,
            warnings=warnings,
        )


def _has_legacy_github_creds(settings: Settings) -> bool:
    """Check if legacy GitHub credentials are set (PAT or App)."""
    has_pat = bool(getattr(settings, "github_token", ""))
    has_app = bool(getattr(settings, "github_app_id", 0) and getattr(settings, "github_app_private_key", ""))
    return has_pat or has_app


def _get_env_value(
    name: str,
    env: dict[str, str],
    dotenv_fallback: dict[str, str | None],
    required: bool = False,
    context: str = "",
) -> str:
    """Get an environment variable value, with dotenv fallback for dynamic names.

    Never returns the actual secret value in error messages.
    """
    value = env.get(name) or dotenv_fallback.get(name) or ""
    if required and not value.strip():
        raise OwnerConfigError(f"Required environment variable {name} is not set{context}")
    return value.strip()


def _env_name_for_owner(owner_id: str, secret_name: str) -> str:
    """Generate conventional env var name: OWNER_<ID>_<NAME>."""
    normalized = owner_id.upper().replace("-", "_")
    return f"OWNER_{normalized}_{secret_name}"


def _build_legacy_owner(
    settings: Settings,
    app_config: AppConfig,
    env: dict[str, str],
    dotenv_fallback: dict[str, str | None],
) -> OwnerContext:
    """Build the legacy owner from current env + top-level YAML config.

    Exactly matches the logic of get_app_config() for backward compatibility.
    """
    has_pat = bool(getattr(settings, "github_token", ""))

    if has_pat:
        github_creds = GitHubCredentials(
            kind="pat",
            token=settings.github_token,
        )
    else:
        pem = settings.github_private_key_pem()
        github_creds = GitHubCredentials(
            kind="app",
            app_id=settings.github_app_id,
            private_key_pem=pem,
            installation_id=settings.github_installation_id,
        )

    jira_binding = None
    jira_yaml = app_config.jira
    # Match get_app_config() logic: settings override YAML if non-empty
    jira_base = settings.jira_base_url or jira_yaml.base_url
    jira_email = settings.jira_email or jira_yaml.email
    jira_token = settings.jira_api_token
    if jira_base and jira_email:
        jira_binding = JiraBinding(
            base_url=jira_base.rstrip("/"),
            email=jira_email,
            api_token=jira_token,
            projects={k: v.rework_status for k, v in jira_yaml.projects.items()},
        )

    slack_binding = None
    slack_yaml = app_config.slack
    if slack_yaml.enabled:
        slack_binding = SlackBinding(
            enabled=slack_yaml.enabled,
            channel=slack_yaml.channel,
            bot_token=settings.slack_bot_token,
        )

    return OwnerContext(
        id=DEFAULT_OWNER_ID,
        is_default=True,
        github=github_creds,
        webhook_secret=settings.github_webhook_secret,
        api_key=settings.review_api_key,
        jira=jira_binding,
        slack=slack_binding,
        openai_api_key=settings.openai_api_key,
        config=app_config,
    )


def _build_owner_context(
    owner_id: str,
    owner_yaml: OwnerYaml,
    app_config: AppConfig,
    env: dict[str, str],
    dotenv_fallback: dict[str, str | None],
    settings: Settings,
    warnings: list[str],
) -> OwnerContext:
    """Build an OwnerContext from the owners YAML block."""
    context_msg = f" for owner '{owner_id}'"
    github_yaml = owner_yaml.github

    if github_yaml.auth == "pat":
        token_env = github_yaml.token_env or _env_name_for_owner(owner_id, "GITHUB_TOKEN")
        token = _get_env_value(token_env, env, dotenv_fallback, required=True, context=context_msg)
        github_creds = GitHubCredentials(kind="pat", token=token)
    else:
        if github_yaml.app_id is not None:
            app_id = github_yaml.app_id
        elif github_yaml.app_id_env:
            app_id_str = _get_env_value(
                github_yaml.app_id_env, env, dotenv_fallback, required=True, context=context_msg
            )
            try:
                app_id = int(app_id_str)
            except ValueError:
                raise OwnerConfigError(f"Invalid app_id value in {github_yaml.app_id_env}{context_msg}") from None
        else:
            app_id_env = _env_name_for_owner(owner_id, "GITHUB_APP_ID")
            app_id_str = _get_env_value(app_id_env, env, dotenv_fallback, required=True, context=context_msg)
            try:
                app_id = int(app_id_str)
            except ValueError:
                raise OwnerConfigError(f"Invalid app_id value in {app_id_env}{context_msg}") from None

        if github_yaml.private_key_file:
            key_path = Path(github_yaml.private_key_file)
            if not key_path.is_file():
                raise OwnerConfigError(f"Private key file does not exist: {github_yaml.private_key_file}{context_msg}")
            pem = key_path.read_text(encoding="utf-8")
        else:
            key_env = github_yaml.private_key_env or _env_name_for_owner(owner_id, "GITHUB_APP_PRIVATE_KEY")
            raw_key = _get_env_value(key_env, env, dotenv_fallback, required=True, context=context_msg)
            pem = raw_key.replace("\\n", "\n").strip()
            if not pem.startswith("-----"):
                key_path = Path(pem)
                if key_path.is_file():
                    pem = key_path.read_text(encoding="utf-8")

        github_creds = GitHubCredentials(
            kind="app",
            app_id=app_id,
            private_key_pem=pem,
            installation_id=github_yaml.installation_id,
        )

    webhook_secret: str | None = None
    if github_yaml.webhook_secret_env:
        webhook_secret = _get_env_value(github_yaml.webhook_secret_env, env, dotenv_fallback)
    else:
        default_secret_env = _env_name_for_owner(owner_id, "GITHUB_WEBHOOK_SECRET")
        webhook_secret = _get_env_value(default_secret_env, env, dotenv_fallback)

    if not webhook_secret or len(webhook_secret) < 16:
        warnings.append(f"Owner '{owner_id}' has no valid webhook secret; webhooks will be disabled for this owner.")

    api_key: str | None = None
    if owner_yaml.api.key_env:
        api_key = _get_env_value(owner_yaml.api.key_env, env, dotenv_fallback)
    else:
        default_api_env = _env_name_for_owner(owner_id, "REVIEW_API_KEY")
        api_key = _get_env_value(default_api_env, env, dotenv_fallback)

    jira_binding: JiraBinding | None = None
    if owner_yaml.jira is not None:
        jira_yaml = owner_yaml.jira
        token_env = jira_yaml.api_token_env or _env_name_for_owner(owner_id, "JIRA_API_TOKEN")
        jira_token = _get_env_value(token_env, env, dotenv_fallback)
        if jira_yaml.base_url and jira_yaml.email:
            jira_binding = JiraBinding(
                base_url=jira_yaml.base_url.rstrip("/"),
                email=jira_yaml.email,
                api_token=jira_token,
                projects={k: v.rework_status for k, v in jira_yaml.projects.items()},
            )
            if not jira_token:
                warnings.append(f"Owner '{owner_id}' has Jira configured but no API token; Jira will be disabled.")

    slack_binding: SlackBinding | None = None
    if owner_yaml.slack is not None:
        slack_yaml = owner_yaml.slack
        token_env = slack_yaml.bot_token_env or _env_name_for_owner(owner_id, "SLACK_BOT_TOKEN")
        slack_token = _get_env_value(token_env, env, dotenv_fallback)
        slack_binding = SlackBinding(
            enabled=slack_yaml.enabled,
            channel=slack_yaml.channel,
            bot_token=slack_token,
        )
        if slack_yaml.enabled and not slack_token:
            warnings.append(f"Owner '{owner_id}' has Slack enabled but no bot token; Slack will be disabled.")

    openai_key: str | None = None
    if owner_yaml.codex and owner_yaml.codex.api_key_env:
        openai_key = _get_env_value(owner_yaml.codex.api_key_env, env, dotenv_fallback)
    if not openai_key:
        openai_key = settings.openai_api_key

    effective_config = _merge_owner_config(app_config, owner_yaml)

    if not github_yaml.allowed_repos:
        warnings.append(f"Owner '{owner_id}' has empty allowed_repos: accepts any repository routed to it.")

    return OwnerContext(
        id=owner_id,
        is_default=owner_yaml.default,
        github=github_creds,
        webhook_secret=webhook_secret if webhook_secret else None,
        api_key=api_key if api_key else None,
        jira=jira_binding,
        slack=slack_binding,
        openai_api_key=openai_key,
        config=effective_config,
    )


def _merge_owner_config(app_config: AppConfig, owner_yaml: OwnerYaml) -> AppConfig:
    """Create effective AppConfig for owner by deep-merging overrides.

    For M1, we just return the base config (no per-owner overrides yet).
    M3 will implement the actual deep merge of public_repos, language, features, etc.
    """
    return app_config


def _validate_claims(allowed_repos_map: dict[str, list[str]], warnings: list[str]) -> None:
    """Validate that no two owners claim the same repo or wildcard."""
    exact_claims: dict[str, str] = {}
    wildcard_claims: dict[str, str] = {}

    for owner_id, allowed in allowed_repos_map.items():
        for repo in allowed:
            repo_lower = repo.lower()
            if repo_lower.endswith("/*"):
                org = repo_lower[:-2]
                if org in wildcard_claims:
                    raise OwnerConfigError(
                        f"Wildcard '{repo}' is claimed by both '{wildcard_claims[org]}' and '{owner_id}'"
                    )
                wildcard_claims[org] = owner_id
            else:
                if repo_lower in exact_claims:
                    raise OwnerConfigError(
                        f"Repository '{repo}' is claimed by both '{exact_claims[repo_lower]}' and '{owner_id}'"
                    )
                exact_claims[repo_lower] = owner_id

    for repo_lower, exact_owner in exact_claims.items():
        org = repo_lower.split("/", 1)[0]
        if org in wildcard_claims:
            wildcard_owner = wildcard_claims[org]
            if exact_owner != wildcard_owner:
                warnings.append(
                    f"Repository '{repo_lower}' is claimed exactly by '{exact_owner}' "
                    f"and via wildcard '{org}/*' by '{wildcard_owner}'; exact claim takes precedence."
                )


def _validate_api_keys(owners: dict[str, OwnerContext]) -> None:
    """Validate that no two owners have the same API key."""
    key_to_owner: dict[str, str] = {}
    for owner_id, ctx in owners.items():
        if ctx.api_key:
            if ctx.api_key in key_to_owner:
                raise OwnerConfigError(f"API key is used by both '{key_to_owner[ctx.api_key]}' and '{owner_id}'")
            key_to_owner[ctx.api_key] = owner_id


def _validate_credentials_uniqueness(owners: dict[str, OwnerContext], warnings: list[str]) -> None:
    """Validate credentials uniqueness: same (app_id, installation_id) is an error; same PAT is a warning."""
    app_creds: dict[tuple[int, int], str] = {}
    pat_hashes: dict[str, list[str]] = {}

    for owner_id, ctx in owners.items():
        creds = ctx.github
        if creds.kind == "app" and creds.app_id and creds.installation_id:
            key = (creds.app_id, creds.installation_id)
            if key in app_creds:
                raise OwnerConfigError(
                    f"Same (app_id={key[0]}, installation_id={key[1]}) is used by "
                    f"both '{app_creds[key]}' and '{owner_id}'"
                )
            app_creds[key] = owner_id
        elif creds.kind == "pat" and creds.token and isinstance(creds.token, str):
            import hashlib

            token_hash = hashlib.sha256(creds.token.encode()).hexdigest()[:16]
            if token_hash in pat_hashes:
                pat_hashes[token_hash].append(owner_id)
            else:
                pat_hashes[token_hash] = [owner_id]

    for _token_hash, owner_ids in pat_hashes.items():
        if len(owner_ids) > 1:
            warnings.append(
                f"Same GitHub PAT is used by owners: {', '.join(owner_ids)}. "
                "Comments will share the same author identity."
            )


@lru_cache
def get_owner_registry() -> OwnerRegistry:
    """Get the cached owner registry, building it on first access."""
    from app.config import get_app_config, get_settings

    settings = get_settings()
    app_config = get_app_config()
    return OwnerRegistry.build(settings, app_config)
