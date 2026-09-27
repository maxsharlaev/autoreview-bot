"""Decide whether a PR can use the model from GitHub PR size metadata."""

from __future__ import annotations

from typing import Literal

from app.adapters.github import PullRequestInfo
from app.config import SizeGuardYaml

SIZE_SKIP_MARKER = "<!-- autoreview-bot:size-guard-skip -->"
SizeDecision = Literal["normal", "soft", "hard"]


def classify_pr_size(info: PullRequestInfo, guard: SizeGuardYaml) -> SizeDecision:
    if guard.override_label.casefold() in {label.casefold() for label in info.labels}:
        return "normal"
    changed_lines = info.additions + info.deletions
    if info.commits_count > guard.hard.commits or changed_lines > guard.hard.changed_lines:
        return "hard"
    if info.commits_count > guard.soft.commits or changed_lines > guard.soft.changed_lines:
        return "soft"
    return "normal"


def hard_skip_comment(info: PullRequestInfo, *, override_label: str = "autoreview:force") -> str:
    return (
        f"{SIZE_SKIP_MARKER}\n"
        "AI review skipped: this PR exceeds the configured hard size limit "
        f"({info.commits_count} commits, {info.additions + info.deletions} changed lines, "
        f"{info.changed_files} changed files). Split the PR or add `{override_label}` "
        "to request a review.\n"
    )
