"""GitHub REST adapter (App installation token or fine-grained PAT)."""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

from app.config import Settings, get_settings
from app.services.visibility import Visibility, repository_visibility

logger = logging.getLogger(__name__)

API = "https://api.github.com"
MARKER = "<!-- open-pr-review -->"
_COMMENT_AUTHOR_LOGINS: dict[str, str] = {}


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class PullRequestInfo:
    number: int
    title: str
    body: str
    html_url: str
    author: str
    assignee: str | None
    state: str
    draft: bool
    is_fork: bool
    base_sha: str
    head_sha: str
    head_ref: str
    base_ref: str
    owner: str
    repo: str
    full_name: str
    commits_count: int = 0
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    labels: tuple[str, ...] = ()
    visibility: Visibility | None = None


@dataclass
class ChangedFile:
    path: str
    status: str
    patch: str
    additions: int
    deletions: int


@dataclass
class GitHubAppClient:
    settings: Settings = field(default_factory=get_settings)
    previous_comment_authors: tuple[str, ...] = ()
    _token: str | None = None
    _token_expires: float = 0.0
    _installation_id: int = 0

    def _personal_token(self) -> str:
        return (self.settings.github_token or "").strip()

    def _jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": str(self.settings.github_app_id)}
        return jwt.encode(payload, self.settings.github_private_key_pem(), algorithm="RS256")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        json: dict | None = None,
        params: dict | None = None,
        accept: str = "application/vnd.github+json",
    ) -> httpx.Response:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "open-pr-review",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(method, url, headers=headers, json=json, params=params)
        if response.status_code >= 400:
            raise GitHubError(
                f"{method} {url} -> {response.status_code}: {response.text[:500]}", status_code=response.status_code
            )
        return response

    async def resolve_installation_id(self, owner: str, repo: str) -> int:
        if self._installation_id:
            return self._installation_id
        configured = self.settings.github_installation_id
        if configured:
            self._installation_id = configured
            return configured
        response = await self._request(
            "GET",
            f"{API}/repos/{owner}/{repo}/installation",
            token=self._jwt(),
        )
        self._installation_id = int(response.json()["id"])
        return self._installation_id

    async def installation_token(self, owner: str, repo: str) -> str:
        pat = self._personal_token()
        if pat:
            return pat
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        if not self.settings.github_app_id or not self.settings.github_app_private_key.strip():
            raise GitHubError("GitHub credentials missing: set GITHUB_TOKEN (PAT) or GitHub App id + private key")
        installation_id = await self.resolve_installation_id(owner, repo)
        response = await self._request(
            "POST",
            f"{API}/app/installations/{installation_id}/access_tokens",
            token=self._jwt(),
        )
        data = response.json()
        self._token = data["token"]
        self._token_expires = time.time() + 3500
        return self._token

    async def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequestInfo:
        token = await self.installation_token(owner, repo)
        data = (await self._request("GET", f"{API}/repos/{owner}/{repo}/pulls/{number}", token=token)).json()
        head_repo = data.get("head", {}).get("repo") or {}
        base_repo = data.get("base", {}).get("repo") or {}
        is_fork = bool(head_repo.get("fork")) or head_repo.get("full_name") != base_repo.get("full_name")
        assignee = None
        if data.get("assignee"):
            assignee = data["assignee"].get("login")
        elif data.get("assignees"):
            assignee = data["assignees"][0].get("login")
        return PullRequestInfo(
            number=data["number"],
            title=data.get("title") or "",
            body=data.get("body") or "",
            html_url=data.get("html_url") or "",
            author=(data.get("user") or {}).get("login") or "",
            assignee=assignee,
            state=data.get("state") or "open",
            draft=bool(data.get("draft")),
            is_fork=is_fork,
            base_sha=data.get("base", {}).get("sha") or "",
            head_sha=data.get("head", {}).get("sha") or "",
            head_ref=data.get("head", {}).get("ref") or "",
            base_ref=data.get("base", {}).get("ref") or "",
            owner=owner,
            repo=repo,
            full_name=f"{owner}/{repo}",
            commits_count=int(data.get("commits") or 0),
            additions=int(data.get("additions") or 0),
            deletions=int(data.get("deletions") or 0),
            changed_files=int(data.get("changed_files") or 0),
            labels=tuple(str(item.get("name") or "") for item in data.get("labels") or []),
            visibility=repository_visibility(base_repo),
        )

    async def list_files(self, owner: str, repo: str, number: int) -> list[ChangedFile]:
        token = await self.installation_token(owner, repo)
        files: list[ChangedFile] = []
        page = 1
        while True:
            response = await self._request(
                "GET",
                f"{API}/repos/{owner}/{repo}/pulls/{number}/files",
                token=token,
                params={"per_page": 100, "page": page},
            )
            batch = response.json()
            if not batch:
                break
            for item in batch:
                files.append(
                    ChangedFile(
                        path=item.get("filename") or "",
                        status=item.get("status") or "modified",
                        patch=item.get("patch") or "",
                        additions=int(item.get("additions") or 0),
                        deletions=int(item.get("deletions") or 0),
                    )
                )
            if len(batch) < 100:
                break
            page += 1
        return files

    async def list_pull_commit_messages(self, owner: str, repo: str, number: int) -> list[str]:
        token = await self.installation_token(owner, repo)
        messages: list[str] = []
        page = 1
        while page <= 3 and len(messages) < 250:
            response = await self._request(
                "GET",
                f"{API}/repos/{owner}/{repo}/pulls/{number}/commits",
                token=token,
                params={"per_page": 100, "page": page},
            )
            batch = response.json()
            messages.extend(
                str((item.get("commit") or {}).get("message") or "") for item in batch[: 250 - len(messages)]
            )
            if len(batch) < 100 or len(messages) >= 250:
                break
            page += 1
        return messages

    async def get_default_pr_template(self, owner: str, repo: str, base_ref: str) -> str | None:
        token = await self.installation_token(owner, repo)
        for directory in (".github/", "", "docs/"):
            for filename in ("pull_request_template.md", "PULL_REQUEST_TEMPLATE.md"):
                path = directory + filename
                try:
                    response = await self._request(
                        "GET",
                        f"{API}/repos/{owner}/{repo}/contents/{path}",
                        token=token,
                        params={"ref": base_ref},
                    )
                except GitHubError as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                data = response.json()
                if data.get("encoding") == "base64":
                    return base64.b64decode(data.get("content") or "").decode("utf-8")
        return None

    async def get_matching_pr_template(self, owner: str, repo: str, base_ref: str, body: str) -> str | None:
        normalized_body = body.replace("\r\n", "\n").strip()
        default = await self.get_default_pr_template(owner, repo, base_ref)
        if default is not None and default.replace("\r\n", "\n").strip() == normalized_body:
            return default
        token = await self.installation_token(owner, repo)
        for directory in (".github/PULL_REQUEST_TEMPLATE", ".github/pull_request_template"):
            try:
                response = await self._request(
                    "GET",
                    f"{API}/repos/{owner}/{repo}/contents/{directory}",
                    token=token,
                    params={"ref": base_ref},
                )
            except GitHubError as exc:
                if exc.status_code == 404:
                    continue
                raise
            entries = response.json()
            if not isinstance(entries, list):
                continue
            for entry in entries[:30]:
                if entry.get("type") != "file" or not str(entry.get("name") or "").lower().endswith(".md"):
                    continue
                file_response = await self._request(
                    "GET",
                    f"{API}/repos/{owner}/{repo}/contents/{entry['path']}",
                    token=token,
                    params={"ref": base_ref},
                )
                data = file_response.json()
                if data.get("encoding") != "base64":
                    continue
                template = base64.b64decode(data.get("content") or "").decode("utf-8")
                if template.replace("\r\n", "\n").strip() == normalized_body:
                    return template
        return None

    async def update_pull_request_body(self, owner: str, repo: str, number: int, body: str) -> None:
        token = await self.installation_token(owner, repo)
        await self._request("PATCH", f"{API}/repos/{owner}/{repo}/pulls/{number}", token=token, json={"body": body})

    async def update_pull_request_title(self, owner: str, repo: str, number: int, title: str) -> None:
        token = await self.installation_token(owner, repo)
        await self._request("PATCH", f"{API}/repos/{owner}/{repo}/pulls/{number}", token=token, json={"title": title})

    async def collaborator_permission(self, owner: str, repo: str, username: str) -> str:
        token = await self.installation_token(owner, repo)
        try:
            response = await self._request(
                "GET",
                f"{API}/repos/{owner}/{repo}/collaborators/{username}/permission",
                token=token,
            )
        except GitHubError:
            return "none"
        return (response.json().get("permission") or "none").lower()

    async def comment_author_login(self, token: str) -> str:
        personal = bool(self._personal_token())
        cache_key = (
            f"pat:{hashlib.sha256(token.encode()).hexdigest()}" if personal else f"app:{self.settings.github_app_id}"
        )
        if cached := _COMMENT_AUTHOR_LOGINS.get(cache_key):
            return cached
        if personal:
            data = (await self._request("GET", f"{API}/user", token=token)).json()
            login = str(data.get("login") or "")
        else:
            data = (await self._request("GET", f"{API}/app", token=self._jwt())).json()
            slug = str(data.get("slug") or "")
            login = f"{slug}[bot]" if slug else ""
        if not login:
            raise GitHubError("Could not identify GitHub comment author")
        _COMMENT_AUTHOR_LOGINS[cache_key] = login
        return login

    async def upsert_sticky_comment(
        self,
        owner: str,
        repo: str,
        number: int,
        body: str,
        *,
        marker: str = MARKER,
        can_replace: Callable[[str], bool] | None = None,
    ) -> None:
        token = await self.installation_token(owner, repo)
        try:
            author_login = (await self.comment_author_login(token)).casefold()
        except Exception as exc:
            logger.warning("Could not verify sticky comment author; using marker lookup: %s", exc)
            author_login = None
        trusted_logins = {login.casefold() for login in self.previous_comment_authors if login.strip()}
        if author_login is not None:
            trusted_logins.add(author_login)
        page = 1
        comment_id: int | None = None
        while True:
            response = await self._request(
                "GET",
                f"{API}/repos/{owner}/{repo}/issues/{number}/comments",
                token=token,
                params={"per_page": 100, "page": page},
            )
            batch = response.json()
            if not batch:
                break
            for comment in batch:
                comment_login = str((comment.get("user") or {}).get("login") or "").casefold()
                if author_login is not None and comment_login not in trusted_logins:
                    continue
                if marker in (comment.get("body") or ""):
                    if can_replace is not None and not can_replace(comment.get("body") or ""):
                        logger.info("Sticky comment was edited; leaving it unchanged")
                        return
                    comment_id = int(comment["id"])
                    break
            if comment_id or len(batch) < 100:
                break
            page += 1
        payload = {"body": body}
        if comment_id:
            await self._request(
                "PATCH",
                f"{API}/repos/{owner}/{repo}/issues/comments/{comment_id}",
                token=token,
                json=payload,
            )
            return
        await self._request(
            "POST",
            f"{API}/repos/{owner}/{repo}/issues/{number}/comments",
            token=token,
            json=payload,
        )

    async def list_open_pulls(self, owner: str, repo: str) -> list[dict[str, Any]]:
        token = await self.installation_token(owner, repo)
        response = await self._request(
            "GET",
            f"{API}/repos/{owner}/{repo}/pulls",
            token=token,
            params={"state": "open", "per_page": 100},
        )
        return response.json()
