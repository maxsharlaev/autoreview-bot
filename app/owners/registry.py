"""Owner registry: builds and manages owner contexts with startup validation."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from app.config import RoutingYaml
from app.owners.context import GitHubCredentials, JiraBinding, OwnerContext, SlackBinding
from app.owners.schema import OwnerYaml, validate_owner_id

# Routing reasons for RouteResult
ROUTE_EXPLICIT = "explicit"
ROUTE_EXACT = "exact"
ROUTE_WILDCARD = "wildcard"
ROUTE_INSTALLATION = "installation"
ROUTE_DEFAULT_FALLBACK = "default_fallback"

# Rejection reasons
REJECT_UNKNOWN_OWNER = "unknown_owner"
REJECT_REPO_NOT_ALLOWED = "repo_not_allowed"
REJECT_OWNER_REPO_CONFLICT = "owner_repo_conflict"
REJECT_OWNER_DISABLED = "owner_disabled"

if TYPE_CHECKING:
    from app.config import AppConfig, Settings

logger = logging.getLogger(__name__)

DEFAULT_OWNER_ID = "default"
ENV_VAR_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class OwnerConfigError(ValueError):
    """Raised when owner configuration is invalid at startup."""


@dataclass(frozen=True)
class RouteResult:
    """Result of routing a repository to an owner."""

    owner_id: str
    reason: str

    def rejected(self) -> bool:
        """Return True if this is a rejection (no owner assigned)."""
        return self.reason in {
            REJECT_UNKNOWN_OWNER,
            REJECT_REPO_NOT_ALLOWED,
            REJECT_OWNER_REPO_CONFLICT,
            REJECT_OWNER_DISABLED,
        }


@dataclass
class OwnerRegistry:
    """Registry of all owners with their contexts."""

    owners: dict[str, OwnerContext]
    aliases: dict[str, str]  # alias -> owner_id
    default_owner_id: str
    routing: RoutingYaml
    warnings: list[str]
    # Claim maps for routing
    exact_claims: dict[str, str] = field(default_factory=dict)  # lowercase full_name -> owner_id
    wildcard_claims: dict[str, str] = field(default_factory=dict)  # lowercase org -> owner_id
    installation_claims: dict[int, str] = field(default_factory=dict)  # installation_id -> owner_id
    # Store owner YAMLs for allowlist lookups
    _owner_yamls: dict[str, OwnerYaml] = field(default_factory=dict, repr=False)
    # Track disabled owners (for owner_disabled vs owner_not_configured distinction)
    disabled_owners: set[str] = field(default_factory=set)
    # Webhook secrets of disabled owners: /pull-request/{owner_id} verifies the signature
    # before answering owner_disabled, so the endpoint does not reveal owner state to anyone.
    disabled_webhook_secrets: dict[str, str] = field(default_factory=dict, repr=False)

    def get(self, owner_id: str) -> OwnerContext | None:
        """Get owner by id or alias (case-insensitive)."""
        owner_lower = owner_id.lower()
        # Try case-insensitive alias match
        for alias, target in self.aliases.items():
            if alias.lower() == owner_lower:
                return self.owners.get(target)
        # Try case-insensitive owner_id match
        for oid, ctx in self.owners.items():
            if oid.lower() == owner_lower:
                return ctx
        return None

    def canonicalize(self, owner_id: str) -> str | None:
        """Resolve alias and case to the canonical owner_id.

        Returns the canonical owner_id or None if the owner doesn't exist.
        Also returns None for disabled owners (caller must check is_disabled).
        """
        owner_lower = owner_id.lower()
        # Try case-insensitive alias match
        for alias, target in self.aliases.items():
            if alias.lower() == owner_lower:
                # Disabled owners return None from canonicalize
                if target in self.disabled_owners:
                    return None
                return target
        # Try case-insensitive owner_id match
        for oid in self.owners:
            if oid.lower() == owner_lower:
                return oid
        # Check disabled owners (return None - caller must use is_disabled)
        for oid in self.disabled_owners:
            if oid.lower() == owner_lower:
                return None
        return None

    def is_disabled(self, owner_id: str) -> bool:
        """Check if owner exists but is disabled (case-insensitive, alias-aware).

        Returns True if the owner (or its alias target) is disabled.
        """
        owner_lower = owner_id.lower()
        # Try alias match
        for alias, target in self.aliases.items():
            if alias.lower() == owner_lower:
                return target in self.disabled_owners
        # Try direct owner_id match
        for oid in self.disabled_owners:
            if oid.lower() == owner_lower:
                return True
        return False

    def legacy_default_alias(self) -> str | None:
        """Owner id that owns rows stored with the legacy owner_id 'default'.

        Rows written before multi-owner support (and by the legacy single-owner
        setup) carry owner_id='default'. The binding is explicit and never follows
        the `default: true` flag:
        - 'default' if an owner literally named 'default' exists (modes A/B);
        - else the enabled owner that lists 'default' in its aliases (modes C/D);
        - else None: legacy rows are visible to no owner.
        """
        if DEFAULT_OWNER_ID in self.owners:
            return DEFAULT_OWNER_ID
        for alias, target in self.aliases.items():
            if alias.lower() == DEFAULT_OWNER_ID and target in self.owners:
                return target
        return None

    def get_default(self) -> OwnerContext | None:
        """Get the default owner context, or None if no owners are configured."""
        if not self.default_owner_id:
            return None
        return self.owners.get(self.default_owner_id)

    def disabled_owner_webhook_secret(self, owner_id: str) -> str | None:
        """Webhook secret of a disabled owner (case-insensitive, alias-aware); None if unknown/unset."""
        owner_lower = owner_id.lower()
        target = None
        for alias, alias_target in self.aliases.items():
            if alias.lower() == owner_lower:
                target = alias_target
                break
        if target is None:
            target = next((oid for oid in self.disabled_owners if oid.lower() == owner_lower), None)
        if target is None or target not in self.disabled_owners:
            return None
        return self.disabled_webhook_secrets.get(target) or None

    def webhook_secrets(self) -> dict[str, str]:
        """Return mapping of owner_id -> webhook_secret for owners with valid secrets."""
        return {owner_id: ctx.webhook_secret for owner_id, ctx in self.owners.items() if ctx.webhook_enabled()}

    def resolve(
        self,
        full_name: str,
        installation_id: int | None = None,
        explicit_owner: str | None = None,
    ) -> RouteResult:
        """Route a repository to an owner.

        Order:
        1. Explicit owner (URL path / header / query / owner-scoped API key)
        2. Exact full_name in some owner's allowed_repos
        3. Wildcard org/* match
        4. installation_id matches an owner's non-zero installation_id
        5. routing.unclaimed: 'default' -> default owner, 'reject' -> unknown_owner

        After selection, applies the owner's allowlist check.

        Returns:
            RouteResult with owner_id and reason (or rejection reason)
        """
        full_name_lower = full_name.lower()
        org = full_name_lower.split("/", 1)[0] if "/" in full_name_lower else ""

        # Step 1: Explicit owner (case-insensitive)
        if explicit_owner:
            explicit_lower = explicit_owner.lower()
            resolved_id: str | None = None
            is_disabled_via_alias = False
            # Try case-insensitive alias match
            for alias, target in self.aliases.items():
                if alias.lower() == explicit_lower:
                    resolved_id = target
                    # Check if alias target is disabled
                    if target in self.disabled_owners:
                        is_disabled_via_alias = True
                    break
            # Try case-insensitive owner_id match
            if resolved_id is None:
                for oid in self.owners:
                    if oid.lower() == explicit_lower:
                        resolved_id = oid
                        break
            # Check if the owner is disabled (return owner_disabled, not unknown_owner)
            if resolved_id is None:
                for oid in self.disabled_owners:
                    if oid.lower() == explicit_lower:
                        return RouteResult("", REJECT_OWNER_DISABLED)
            # Alias resolved to a disabled owner
            if is_disabled_via_alias:
                return RouteResult("", REJECT_OWNER_DISABLED)
            if resolved_id is None:
                return RouteResult("", REJECT_UNKNOWN_OWNER)
            ctx = self.owners.get(resolved_id)
            if ctx is None:
                return RouteResult("", REJECT_UNKNOWN_OWNER)

            # Check if repo is claimed by another owner
            exact_claimer = self.exact_claims.get(full_name_lower)
            wildcard_claimer = self.wildcard_claims.get(org) if org else None
            if exact_claimer and exact_claimer != resolved_id:
                return RouteResult("", REJECT_OWNER_REPO_CONFLICT)
            if wildcard_claimer and wildcard_claimer != resolved_id and not exact_claimer:
                return RouteResult("", REJECT_OWNER_REPO_CONFLICT)

            # Check owner's allowlist
            if not self._repo_allowed_for_owner(ctx, full_name_lower):
                return RouteResult("", REJECT_REPO_NOT_ALLOWED)

            return RouteResult(resolved_id, ROUTE_EXPLICIT)

        # Step 2: Exact claim
        if full_name_lower in self.exact_claims:
            owner_id = self.exact_claims[full_name_lower]
            ctx = self.owners.get(owner_id)
            if ctx is None:
                return RouteResult("", REJECT_UNKNOWN_OWNER)
            return RouteResult(owner_id, ROUTE_EXACT)

        # Step 3: Wildcard claim
        if org and org in self.wildcard_claims:
            owner_id = self.wildcard_claims[org]
            ctx = self.owners.get(owner_id)
            if ctx is None:
                return RouteResult("", REJECT_UNKNOWN_OWNER)
            return RouteResult(owner_id, ROUTE_WILDCARD)

        # Step 4: Installation ID claim
        if installation_id and installation_id in self.installation_claims:
            owner_id = self.installation_claims[installation_id]
            ctx = self.owners.get(owner_id)
            if ctx is None:
                return RouteResult("", REJECT_UNKNOWN_OWNER)
            # Check owner's allowlist
            if not self._repo_allowed_for_owner(ctx, full_name_lower):
                return RouteResult("", REJECT_REPO_NOT_ALLOWED)
            return RouteResult(owner_id, ROUTE_INSTALLATION)

        # Step 5: Unclaimed - use routing config
        if self.routing.unclaimed == "reject":
            return RouteResult("", REJECT_UNKNOWN_OWNER)

        # Default fallback
        if not self.default_owner_id:
            return RouteResult("", REJECT_UNKNOWN_OWNER)

        ctx = self.owners.get(self.default_owner_id)
        if ctx is None:
            return RouteResult("", REJECT_UNKNOWN_OWNER)

        # Check default owner's allowlist
        if not self._repo_allowed_for_owner(ctx, full_name_lower):
            return RouteResult("", REJECT_REPO_NOT_ALLOWED)

        return RouteResult(self.default_owner_id, ROUTE_DEFAULT_FALLBACK)

    def _repo_allowed_for_owner(self, ctx: OwnerContext, full_name_lower: str) -> bool:
        """Check if a repository is allowed by the owner's allowlist.

        Empty allowlist means all repos are allowed.
        """
        # Get owner's allowed_repos from config
        owner_yaml = self._get_owner_yaml(ctx.id)
        if owner_yaml is None:
            # Legacy owner - check global config
            allowed = [item.lower() for item in ctx.config.github.allowed_repos]
        else:
            allowed = [item.lower() for item in owner_yaml.github.allowed_repos]

        if not allowed:
            return True

        org = full_name_lower.split("/", 1)[0] if "/" in full_name_lower else ""
        for pattern in allowed:
            if pattern == full_name_lower:
                return True
            if pattern.endswith("/*") and pattern[:-2] == org:
                return True
        return False

    def _get_owner_yaml(self, owner_id: str) -> OwnerYaml | None:
        """Get the OwnerYaml for a given owner_id (None for legacy owner)."""
        # This is set during build() and stored in _owner_yamls
        return getattr(self, "_owner_yamls", {}).get(owner_id)

    def get_allowed_repos_for_owner(self, owner_id: str) -> list[str] | None:
        """Get the allowed_repos list for an owner.

        Returns:
            - owner's allowed_repos from YAML for named owners
            - None for the legacy default owner (caller should use global config)
        """
        owner_yaml = self._get_owner_yaml(owner_id)
        if owner_yaml is None:
            return None
        return list(owner_yaml.github.allowed_repos)

    def principal_for_key(self, key: str, operator_key: str | None = None) -> tuple[str, str | None]:
        """Identify the principal for an API key.

        Args:
            key: The presented API key
            operator_key: The global REVIEW_API_KEY from settings (operator key)

        Returns:
            (kind, owner_id) where kind is 'operator' (global key) or 'owner' (scoped key)
            Returns ('', None) if key is invalid
        """
        if not key:
            return ("", None)

        # Check operator key (global REVIEW_API_KEY) first
        # This gives access to all owners
        if operator_key and hmac.compare_digest(operator_key, key):
            return ("operator", None)

        # Check owner-scoped keys (constant-time across all keys)
        # These are the api.key_env values from each owner's config
        matched_owner: str | None = None
        for owner_id, ctx in self.owners.items():
            if ctx.api_key and hmac.compare_digest(ctx.api_key, key):
                matched_owner = owner_id

        if matched_owner:
            return ("owner", matched_owner)

        return ("", None)

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
            except OwnerConfigError:
                raise
            except Exception as e:
                # Render Pydantic errors without input values to avoid leaking secrets
                from pydantic import ValidationError

                if isinstance(e, ValidationError):
                    error_str = "; ".join(
                        f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                        for err in e.errors(include_input=False)
                    )
                    raise OwnerConfigError(f"Invalid configuration for owner '{owner_id}': {error_str}") from None
                raise OwnerConfigError(f"Invalid configuration for owner '{owner_id}': {e}") from e

        has_legacy_github = _has_legacy_github_creds(settings)
        has_owners_block = bool(owners_yaml)

        owners: dict[str, OwnerContext] = {}
        aliases: dict[str, str] = {}
        warnings: list[str] = []
        disabled_owners_set: set[str] = set()
        disabled_webhook_secrets: dict[str, str] = {}
        default_owner_id: str | None = None

        if has_legacy_github:
            legacy_ctx = _build_legacy_owner(settings, app_config, env, dotenv_fallback)
            owners[DEFAULT_OWNER_ID] = legacy_ctx
            default_owner_id = DEFAULT_OWNER_ID

        if has_owners_block:
            if DEFAULT_OWNER_ID in owners_yaml:
                raise OwnerConfigError(
                    f"Cannot have owner '{DEFAULT_OWNER_ID}' in owners block when legacy GitHub credentials are set "
                    "(mode B conflict). Either remove legacy env vars or rename the owner."
                )

            # Check for disabled owners with default:true
            for oid, cfg in owners_yaml.items():
                if cfg.default and not cfg.enabled:
                    warnings.append(
                        f"Owner '{oid}' has default=true but enabled=false; it will not be counted as the default."
                    )

            # Only count enabled owners as explicit defaults
            explicit_defaults = [oid for oid, cfg in owners_yaml.items() if cfg.default and cfg.enabled]
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
                    # In mode B, 'default' alias conflicts with the legacy owner
                    if alias == DEFAULT_OWNER_ID and has_legacy_github:
                        raise OwnerConfigError(
                            f"Alias '{DEFAULT_OWNER_ID}' conflicts with the legacy default owner in mode B"
                        )
                    aliases[alias] = owner_id

                if not owner_yaml.enabled:
                    disabled_owners_set.add(owner_id)
                    disabled_secret = _owner_webhook_secret(owner_id, owner_yaml, env, dotenv_fallback)
                    if disabled_secret:
                        disabled_webhook_secrets[owner_id] = disabled_secret
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

                # M1: Warn if override fields are set (they're ignored until M3)
                if owner_yaml.has_overrides():
                    override_fields = []
                    if owner_yaml.public_repos is not None:
                        override_fields.append("public_repos")
                    if owner_yaml.language is not None:
                        override_fields.append("language")
                    if owner_yaml.features is not None:
                        override_fields.append("features")
                    if owner_yaml.codex is not None:
                        override_fields.append("codex")
                    if owner_yaml.pr_description is not None:
                        override_fields.append("pr_description")
                    if owner_yaml.size_guard is not None:
                        override_fields.append("size_guard")
                    warnings.append(
                        f"Owner '{owner_id}' has override fields ({', '.join(override_fields)}) "
                        "that are ignored in M1. Per-owner overrides will be implemented in M3."
                    )

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
            # Mode A without any credentials: warning only, startup proceeds
            # This matches the current behavior on main where missing credentials
            # don't prevent startup - the webhook endpoint will be disabled.
            warnings.append(
                "No GitHub credentials configured. Set GITHUB_TOKEN/GITHUB_PAT or "
                "GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY to enable webhook processing."
            )
            # Return early with empty registry - no owners configured
            return cls(
                owners={},
                aliases={},
                default_owner_id="",
                routing=routing,
                warnings=warnings,
                disabled_owners=set(),
            )

        if default_owner_id is None and owners:
            default_owner_id = next(iter(owners.keys()))

        if not owners:
            # No active owners: either mode A without credentials (warning added above) or an
            # owners block where every owner is disabled. Keep the disabled owners' aliases and
            # config so /pull-request/<alias> and API selectors still answer owner_disabled.
            return cls(
                owners={},
                aliases=aliases,
                default_owner_id="",
                routing=routing,
                warnings=warnings,
                _owner_yamls=owners_yaml,
                disabled_owners=disabled_owners_set,
                disabled_webhook_secrets=disabled_webhook_secrets,
            )

        # Build allowed_repos map for validation
        # For legacy owner, use app_config.github.allowed_repos
        # For named owners, use owner_yaml.github.allowed_repos
        allowed_repos_map: dict[str, list[str]] = {}
        if DEFAULT_OWNER_ID in owners:
            allowed_repos_map[DEFAULT_OWNER_ID] = app_config.github.allowed_repos
        for owner_id, owner_yaml in owners_yaml.items():
            if owner_id in owners:
                allowed_repos_map[owner_id] = owner_yaml.github.allowed_repos

        exact_claims, wildcard_claims = _validate_claims(allowed_repos_map, warnings)
        installation_claims = _build_installation_claims(owners)
        _validate_api_keys(owners, operator_key=settings.review_api_key, has_legacy_owner=has_legacy_github)
        _validate_credentials_uniqueness(owners, warnings)

        return cls(
            owners=owners,
            aliases=aliases,
            default_owner_id=default_owner_id or "",
            routing=routing,
            warnings=warnings,
            exact_claims=exact_claims,
            wildcard_claims=wildcard_claims,
            installation_claims=installation_claims,
            _owner_yamls=owners_yaml,
            disabled_owners=disabled_owners_set,
            disabled_webhook_secrets=disabled_webhook_secrets,
        )


def _has_legacy_github_creds(settings: Settings) -> bool:
    """Check if legacy GitHub credentials are set (PAT or App)."""
    token = (getattr(settings, "github_token", "") or "").strip()
    has_pat = bool(token)
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


def _owner_webhook_secret(
    owner_id: str, owner_yaml: OwnerYaml, env: dict[str, str], dotenv_fallback: dict[str, str | None]
) -> str:
    """Webhook secret from github.webhook_secret_env or OWNER_<ID>_GITHUB_WEBHOOK_SECRET ('' if unset)."""
    name = owner_yaml.github.webhook_secret_env or _env_name_for_owner(owner_id, "GITHUB_WEBHOOK_SECRET")
    return _get_env_value(name, env, dotenv_fallback)


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
    token = (getattr(settings, "github_token", "") or "").strip()
    has_pat = bool(token)

    if has_pat:
        github_creds = GitHubCredentials(
            kind="pat",
            token=token,
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
        config=app_config,
        webhook_secret=settings.github_webhook_secret,
        api_key=settings.review_api_key,
        jira=jira_binding,
        slack=slack_binding,
        openai_api_key=settings.openai_api_key,
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

    webhook_secret = _owner_webhook_secret(owner_id, owner_yaml, env, dotenv_fallback)

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
        else:
            warnings.append(f"Owner '{owner_id}' has a jira block without base_url and email; Jira will be disabled.")

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
        if slack_yaml.enabled and not slack_yaml.channel:
            warnings.append(f"Owner '{owner_id}' has Slack enabled but no channel; Slack will be disabled.")

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
        config=effective_config,
        webhook_secret=webhook_secret if webhook_secret else None,
        api_key=api_key if api_key else None,
        jira=jira_binding,
        slack=slack_binding,
        openai_api_key=openai_key,
    )


def _merge_owner_config(app_config: AppConfig, owner_yaml: OwnerYaml) -> AppConfig:
    """Effective AppConfig for a named owner.

    Integrations are never inherited: `jira` and `slack` come only from the owner's own
    blocks (empty/disabled when absent), so code reading config.jira/config.slack can't
    reach the legacy owner's site or channel. Policy overrides (public_repos, language,
    features, pr_description, size_guard, codex) are not merged yet.
    """
    from app.config import JiraProjectYaml, JiraYaml, SlackYaml

    if owner_yaml.jira is not None:
        jira = JiraYaml(
            base_url=owner_yaml.jira.base_url.rstrip("/"),
            email=owner_yaml.jira.email,
            projects={
                key: JiraProjectYaml(rework_status=project.rework_status)
                for key, project in owner_yaml.jira.projects.items()
            },
        )
    else:
        jira = JiraYaml()
    if owner_yaml.slack is not None:
        slack = SlackYaml(enabled=owner_yaml.slack.enabled, channel=owner_yaml.slack.channel)
    else:
        slack = SlackYaml(enabled=False, channel="")
    return app_config.model_copy(update={"jira": jira, "slack": slack})


def _validate_claims(
    allowed_repos_map: dict[str, list[str]], warnings: list[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Validate that no two owners claim the same repo or wildcard.

    Returns:
        Tuple of (exact_claims, wildcard_claims) maps
    """
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

    return exact_claims, wildcard_claims


def _build_installation_claims(owners: dict[str, OwnerContext]) -> dict[int, str]:
    """Build installation_id claims for routing.

    Only non-zero installation_ids are claims.
    """
    installation_claims: dict[int, str] = {}
    for owner_id, ctx in owners.items():
        if ctx.github.kind == "app" and ctx.github.installation_id:
            inst_id = ctx.github.installation_id
            if inst_id in installation_claims:
                raise OwnerConfigError(
                    f"installation_id={inst_id} is claimed by both '{installation_claims[inst_id]}' and '{owner_id}'"
                )
            installation_claims[inst_id] = owner_id
    return installation_claims


def _validate_api_keys(
    owners: dict[str, OwnerContext],
    operator_key: str | None = None,
    has_legacy_owner: bool = False,
) -> None:
    """Validate API key uniqueness and separation from operator key.

    Checks:
    1. No two owners share the same API key
    2. No owner's API key equals the global operator key (REVIEW_API_KEY)
       - This prevents privilege escalation where an owner-scoped key
         would match as the operator key (which has access to all owners)
       - Exception: In Mode A (legacy only), the default owner's key IS the
         operator key by design - skip this check for the legacy owner
    """
    key_to_owner: dict[str, str] = {}
    for owner_id, ctx in owners.items():
        if ctx.api_key:
            # Check for duplicate keys between owners
            if ctx.api_key in key_to_owner:
                raise OwnerConfigError(f"API key is used by both '{key_to_owner[ctx.api_key]}' and '{owner_id}'")
            key_to_owner[ctx.api_key] = owner_id

            # Check that owner key doesn't equal operator key (privilege escalation)
            # Skip for legacy default owner (Mode A) since its api_key IS the operator key
            is_legacy_default = has_legacy_owner and owner_id == DEFAULT_OWNER_ID
            if operator_key and not is_legacy_default and hmac.compare_digest(operator_key, ctx.api_key):
                raise OwnerConfigError(
                    f"Owner '{owner_id}' has an API key that equals the global REVIEW_API_KEY. "
                    "Owner-scoped keys must be distinct from the operator key."
                )


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


LEGACY_DEFAULT_UNBOUND_WARNING = (
    "Owner config: no owner is bound to legacy owner_id 'default'. Findings, review history "
    "and comment authors recorded before multi-owner support stay hidden from every owner. "
    "Add `aliases: [default]` to the owner that should inherit them."
)


def warn_if_legacy_default_unbound(registry: OwnerRegistry, log: logging.Logger) -> bool:
    """Log the startup warning when named owners exist but none is bound to legacy 'default'.

    Shared by API and worker startup so both emit the same text. Returns True if it warned.
    """
    if registry.owners and registry.legacy_default_alias() is None:
        log.warning(LEGACY_DEFAULT_UNBOUND_WARNING)
        return True
    return False


@lru_cache
def get_owner_registry() -> OwnerRegistry:
    """Get the cached owner registry, building it on first access."""
    from app.config import get_app_config, get_settings

    settings = get_settings()
    app_config = get_app_config()
    return OwnerRegistry.build(settings, app_config)
