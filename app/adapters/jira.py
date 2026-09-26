"""Jira Cloud adapter: read issue, comment, transition status."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import AppConfig, get_app_config, get_settings

logger = logging.getLogger(__name__)


class JiraError(RuntimeError):
    pass


@dataclass
class JiraIssue:
    key: str
    summary: str
    description: str
    status: str
    issue_type: str
    priority: str
    acceptance_criteria: str


def adf_to_text(node: object) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(adf_to_text(item) for item in node if adf_to_text(item))
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text") or "")
        content = node.get("content") or []
        text = "\n".join(part for item in content if (part := adf_to_text(item)))
        if node.get("type") in {"paragraph", "heading", "listItem", "blockquote"}:
            return text + "\n"
        return text
    return ""


class JiraClient:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.settings = get_settings()
        self.config = config or get_app_config()
        self.base_url = (self.config.jira.base_url or self.settings.jira_base_url).rstrip("/")
        self.email = self.config.jira.email or self.settings.jira_email
        self.token = self.settings.jira_api_token

    def enabled(self) -> bool:
        return bool(self.base_url and self.email and self.token)

    def project_allowed(self, issue_key: str) -> bool:
        projects = self.config.jira.projects
        if not projects:
            return True
        prefix = issue_key.split("-", 1)[0]
        return prefix in projects

    def rework_status(self, issue_key: str) -> str:
        prefix = issue_key.split("-", 1)[0]
        project = self.config.jira.projects.get(prefix)
        if project:
            return project.rework_status
        return "In Progress"

    def _auth(self) -> tuple[str, str]:
        return self.email, self.token

    async def get_issue(self, issue_key: str) -> JiraIssue:
        if not self.enabled():
            raise JiraError("Jira is not configured")
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        params = {"fields": "summary,description,status,issuetype,priority,comment"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params, auth=self._auth())
        if response.status_code >= 400:
            raise JiraError(f"get_issue {issue_key} -> {response.status_code}")
        data = response.json()
        fields = data.get("fields") or {}
        description = adf_to_text(fields.get("description"))
        return JiraIssue(
            key=data.get("key") or issue_key,
            summary=fields.get("summary") or "",
            description=description.strip(),
            status=((fields.get("status") or {}).get("name") or ""),
            issue_type=((fields.get("issuetype") or {}).get("name") or ""),
            priority=((fields.get("priority") or {}).get("name") or ""),
            acceptance_criteria=description.strip(),
        )

    async def add_comment(self, issue_key: str, body: str) -> None:
        if not self.enabled():
            raise JiraError("Jira is not configured")
        url = f"{self.base_url}/rest/api/2/issue/{issue_key}/comment"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json={"body": body}, auth=self._auth())
        if response.status_code >= 400:
            raise JiraError(f"add_comment {issue_key} -> {response.status_code}: {response.text[:300]}")

    async def transition_to(self, issue_key: str, status_name: str) -> bool:
        if not self.enabled():
            raise JiraError("Jira is not configured")
        async with httpx.AsyncClient(timeout=30.0) as client:
            transitions_url = f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions"
            listing = await client.get(transitions_url, auth=self._auth())
            if listing.status_code >= 400:
                raise JiraError(f"list transitions {issue_key} -> {listing.status_code}")
            target = status_name.strip().lower()
            match: dict | None = None
            for item in listing.json().get("transitions") or []:
                to_name = ((item.get("to") or {}).get("name") or item.get("name") or "").strip().lower()
                if to_name == target:
                    match = item
                    break
            if match is None:
                logger.info("No Jira transition to %s for %s", status_name, issue_key)
                return False
            response = await client.post(
                transitions_url,
                json={"transition": {"id": match["id"]}},
                auth=self._auth(),
            )
        if response.status_code >= 400:
            raise JiraError(f"transition {issue_key} -> {response.status_code}: {response.text[:300]}")
        return True
