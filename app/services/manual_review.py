from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.adapters.github import PullRequestInfo

_PULL_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", re.IGNORECASE)
_REPO_URL = re.compile(r"github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE)


class ManualReviewRequest(BaseModel):
    """Start a review without a GitHub webhook. SHA fields are optional: the worker loads the PR via PAT/App."""

    repository: str | None = None
    number: int | None = Field(default=None, ge=1)
    pull_url: str | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    head_ref: str | None = None
    title: str | None = None
    html_url: str | None = None
    body: str | None = None
    force: bool = True

    @model_validator(mode="after")
    def resolve_repository_and_number(self) -> ManualReviewRequest:
        if self.pull_url:
            match = _PULL_URL.search(self.pull_url.strip())
            if not match:
                raise ValueError("pull_url must look like https://github.com/owner/repo/pull/123")
            self.repository = f"{match.group(1)}/{match.group(2)}"
            self.number = int(match.group(3))
        if not self.repository or not self.number:
            raise ValueError("provide repository and number, or pull_url")
        self.repository = normalize_repo_full_name(self.repository)
        if self.head_sha:
            self.head_sha = self.head_sha.strip()
        if self.base_sha:
            self.base_sha = self.base_sha.strip()
        return self


def normalize_repo_full_name(value: str) -> str:
    cleaned = value.strip().removeprefix("https://").removeprefix("http://")
    cleaned = cleaned.removeprefix("www.")
    match = _REPO_URL.search(cleaned)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    cleaned = cleaned.strip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    parts = [part for part in cleaned.split("/") if part]
    if len(parts) != 2:
        raise ValueError("repository must be owner/name")
    return f"{parts[0]}/{parts[1]}"


def pr_payload_from_request(body: ManualReviewRequest, *, info: PullRequestInfo | None = None) -> dict[str, Any]:
    full_name = body.repository or ""
    number = body.number or 0
    if info is not None:
        return {
            "number": info.number,
            "title": body.title or info.title,
            "html_url": body.html_url or info.html_url,
            "body": body.body if body.body is not None else info.body,
            "draft": info.draft,
            "state": info.state,
            "user": {"login": info.author},
            "assignee": {"login": info.assignee} if info.assignee else None,
            "head": {
                "sha": info.head_sha,
                "ref": info.head_ref,
                "repo": {"full_name": info.full_name, "fork": info.is_fork},
            },
            "base": {"sha": info.base_sha, "repo": {"full_name": info.full_name}},
        }
    html_url = body.html_url or body.pull_url or f"https://github.com/{full_name}/pull/{number}"
    return {
        "number": number,
        "title": body.title or "",
        "html_url": html_url,
        "body": body.body or "",
        "draft": False,
        "state": "open",
        "user": {"login": ""},
        "head": {
            "sha": body.head_sha or "",
            "ref": body.head_ref or "",
            "repo": {"full_name": full_name, "fork": False},
        },
        "base": {"sha": body.base_sha or "", "repo": {"full_name": full_name}},
    }
