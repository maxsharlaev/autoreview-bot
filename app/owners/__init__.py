"""Multi-owner support: owner registry, context, and schema."""

from app.owners.context import (
    GitHubCredentials,
    JiraBinding,
    OwnerContext,
    SlackBinding,
)
from app.owners.registry import (
    REJECT_OWNER_DISABLED,
    REJECT_OWNER_REPO_CONFLICT,
    REJECT_REPO_NOT_ALLOWED,
    REJECT_UNKNOWN_OWNER,
    ROUTE_DEFAULT_FALLBACK,
    ROUTE_EXACT,
    ROUTE_EXPLICIT,
    ROUTE_INSTALLATION,
    ROUTE_WILDCARD,
    OwnerRegistry,
    RouteResult,
    get_owner_registry,
)
from app.owners.schema import (
    OwnerApiYaml,
    OwnerGitHubYaml,
    OwnerJiraYaml,
    OwnerSlackYaml,
    OwnerYaml,
)

__all__ = [
    "GitHubCredentials",
    "JiraBinding",
    "OwnerContext",
    "OwnerRegistry",
    "OwnerApiYaml",
    "OwnerGitHubYaml",
    "OwnerJiraYaml",
    "OwnerSlackYaml",
    "OwnerYaml",
    "SlackBinding",
    "RouteResult",
    "get_owner_registry",
    "REJECT_OWNER_DISABLED",
    "REJECT_OWNER_REPO_CONFLICT",
    "REJECT_REPO_NOT_ALLOWED",
    "REJECT_UNKNOWN_OWNER",
    "ROUTE_DEFAULT_FALLBACK",
    "ROUTE_EXACT",
    "ROUTE_EXPLICIT",
    "ROUTE_INSTALLATION",
    "ROUTE_WILDCARD",
]
