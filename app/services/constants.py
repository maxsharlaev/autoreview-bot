from __future__ import annotations

REVIEW_MARKER = "<!-- open-pr-review -->"
PROMPT_VERSION = "v4"
POLICY_VERSION = "v1"

SKIP_FORK = "fork_pr"
SKIP_DRAFT = "draft_pr"
SKIP_NO_WRITE = "author_no_write_access"
SKIP_REPO = "repo_not_allowed"
SKIP_TOO_LARGE = "PR_TOO_LARGE_FOR_AI_REVIEW"
ISSUE_UNAVAILABLE = "ISSUE_CONTEXT_UNAVAILABLE"
AI_OUTPUT_INVALID = "AI_OUTPUT_INVALID"
CODEX_UNAVAILABLE = "CODEX_UNAVAILABLE"
CODEX_TIMEOUT = "CODEX_TIMEOUT"
CODEX_QUOTA = "CODEX_QUOTA"
CODEX_AUTH = "CODEX_AUTH"
CODEX_NO_RETRY = frozenset({CODEX_UNAVAILABLE, CODEX_TIMEOUT, CODEX_QUOTA, CODEX_AUTH})
INTERNAL_ERROR = "INTERNAL_ERROR"
GIT_REF_NOT_FOUND = "GIT_REF_NOT_FOUND"
GIT_FORBIDDEN = "GIT_FORBIDDEN"
GITHUB_UNAVAILABLE = "GITHUB_UNAVAILABLE"

WRITE_PERMISSIONS = {"admin", "write", "maintain"}
HANDLED_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review", "labeled"}
ACTIVE_RUN_STATUSES = {"pending", "running", "completed"}
IN_FLIGHT_STATUSES = {"pending", "running"}

SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".tgz",
    ".woff",
    ".woff2",
    ".ico",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
}
SKIP_LOCKFILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "composer.lock",
    "go.sum",
}
