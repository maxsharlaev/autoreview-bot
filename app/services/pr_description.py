"""Build, validate, and safely publish an optional PR description draft."""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Literal

import jsonschema

from app.adapters.github import ChangedFile
from app.adapters.jira import JiraIssue
from app.paths import data_file
from app.services.issue_key import ISSUE_KEY_RE
from app.services.untrusted import strip_boundary_tags

COMMENT_MARKER = "<!-- autoreview-bot:pr-description -->"
BLOCK_END = "<!-- autoreview-bot:pr-description:end -->"
_BLOCK_START = "<!-- autoreview-bot:pr-description:start sha256="
_COMMENT_PATTERN = re.compile(
    r"<!-- autoreview-bot:pr-description -->\n"
    r"<!-- autoreview-bot:pr-description-sha256:([0-9a-f]{64}) -->\n\n"
)
_BLOCK_PATTERN = re.compile(
    r"<!-- autoreview-bot:pr-description:start sha256=([0-9a-f]{64}) -->\n(.*?)\n"
    r"<!-- autoreview-bot:pr-description:end -->",
    re.DOTALL,
)
_BOT_BLOCK = re.compile(
    r"<!-- autoreview-bot:pr-description:start\b[^>]*-->.*?"
    r"(?:<!-- autoreview-bot:pr-description:end -->|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_HEADINGS = {
    "en": ("Summary", "Changes", "Linked task", "Testing", "Notes / risks"),
    "ru": ("Кратко", "Изменения", "Связанная задача", "Проверка", "Примечания и риски"),
}
_PLACEHOLDER_TITLES = {
    "dev",
    "develop",
    "development",
    "test",
    "testing",
    "update",
    "changes",
    "wip",
    "draft",
    "untitled",
    "pr",
    "pull request",
}
_CONVENTIONAL_TITLE = re.compile(r"^[a-z][a-z0-9-]*(?:\([a-z0-9._/-]+\))?!?: \S.+$")
_TITLE_URL = re.compile(r"(?:://|\bmailto:|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|dev|ru|ai|co)\b)", re.IGNORECASE)
_LINK_DOMAIN = re.compile(r"\b([a-z0-9-]+)\.(com|org|net|io|dev|ru|ai|co)\b", re.IGNORECASE)


def strip_format_controls(value: str) -> str:
    return "".join(char for char in value if unicodedata.category(char) != "Cf")


def title_source_hash(
    *, head_sha: str, commits: list[str], files: list[ChangedFile], jira_issue: JiraIssue | None, human_body: str
) -> str:
    source = {
        "head_sha": head_sha,
        "commits": commits,
        "files": [(item.path, item.status, item.additions, item.deletions) for item in files],
        "jira": (jira_issue.key, jira_issue.summary) if jira_issue else None,
        "human_body": human_body,
    }
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _untrusted(tag: str, text: str, limit: int) -> str:
    value = strip_boundary_tags((text or "")[:limit])
    return f"<{tag}>\n{value}\n</{tag}>"


def build_pr_description_context(
    *,
    title: str,
    body: str,
    head_ref: str,
    base_sha: str,
    head_sha: str,
    files: list[ChangedFile],
    commit_messages: list[str],
    jira_issue: JiraIssue | None,
    language: str,
    prompt_file: str = "",
    max_commit_messages: int = 250,
    commit_total: int | None = None,
) -> str:
    custom = prompt_file.strip()
    prompt_path = Path(custom).expanduser() if custom else data_file("prompts", "pr_description.md")
    if custom and not prompt_path.is_file():
        raise FileNotFoundError(f"PR description prompt file does not exist: {prompt_path}")
    language_name = {"en": "English", "ru": "Russian"}.get(language, f"language code {language}")
    instructions = prompt_path.read_text(encoding="utf-8").replace("{{details_language}}", language_name)

    areas: dict[str, list[int]] = {}
    for item in files:
        area = item.path.split("/", 1)[0][:100] or "root"
        counts = areas.setdefault(area, [0, 0, 0])
        counts[0] += 1
        counts[1] += item.additions
        counts[2] += item.deletions
    area_lines = [f"{name}: {count} files, +{added}/-{removed}" for name, (count, added, removed) in areas.items()]
    file_lines = [f"{item.status} {item.path[:500]} (+{item.additions}/-{item.deletions})" for item in files[:100]]
    file_summary = "\n".join(
        [
            f"total_files: {len(files)}",
            f"total_additions: {sum(item.additions for item in files)}",
            f"total_deletions: {sum(item.deletions for item in files)}",
            "areas:",
            *area_lines[:50],
            "files:",
            *file_lines,
            f"files_truncated: {str(len(files) > 100).lower()}",
        ]
    )
    selected_commits = commit_messages[-max_commit_messages:]
    commits = "\n".join(message[:500] for message in selected_commits) or "none"
    total_commits = max(commit_total or 0, len(commit_messages))
    commits_truncated = total_commits > len(selected_commits) or any(len(m) > 500 for m in selected_commits)
    commits = f"total_commits: {total_commits}\ncommits_truncated: {str(commits_truncated).lower()}\n{commits}"
    issue_text = "none"
    if jira_issue is not None:
        issue_text = (
            f"key: {jira_issue.key}\nsummary: {jira_issue.summary}\n"
            f"acceptance_criteria: {jira_issue.acceptance_criteria}"
        )
    return "\n\n".join(
        [
            instructions,
            f"base_sha: {base_sha}",
            f"head_sha: {head_sha}",
            _untrusted("untrusted_pr_title", title, 1000),
            _untrusted("untrusted_pr_body", body, 6000),
            _untrusted("untrusted_branch", head_ref, 500),
            _untrusted("untrusted_commit_messages", commits, 50000),
            _untrusted("untrusted_diff_summary", file_summary, 50000),
            _untrusted("untrusted_jira_issue", issue_text, 6000),
        ]
    )


def validate_pr_description(payload: dict[str, Any]) -> dict[str, str]:
    schema = json.loads(data_file("schemas", "pr_description_output.json").read_text(encoding="utf-8"))
    jsonschema.validate(payload, schema)
    return payload


def title_is_invalid(title: str, head_ref: str) -> bool:
    normalized = re.sub(r"[\s_/-]+", " ", title.strip()).casefold()
    branch = re.sub(r"[\s_/-]+", " ", head_ref.strip()).casefold()
    return not normalized or normalized in _PLACEHOLDER_TITLES or normalized == branch


def plan_pr_title_update(
    title: str,
    suggested: str,
    *,
    head_ref: str,
    mode: Literal["off", "until_human_edit", "when_invalid_or_inconsistent"],
    check_relevance: bool,
    relevance: Literal["relevant", "irrelevant", "uncertain"],
    bot_title: str | None = None,
    source_unchanged: bool = False,
) -> str | None:
    if mode == "off":
        return None
    branch_title = title.strip().casefold() == head_ref.strip().casefold()
    editable = not title.strip() or branch_title
    editable = editable or bool(bot_title and title == bot_title)
    if not editable or (source_unchanged and bot_title == title):
        return None
    candidate = strip_format_controls(suggested).strip()
    key = ISSUE_KEY_RE.search(title)
    if key:
        candidate_keys = ISSUE_KEY_RE.findall(candidate)
        if any(candidate_key != key.group(1) for candidate_key in candidate_keys):
            return None
        if key.group(1) not in candidate_keys:
            candidate += f" {key.group(1)}"
    if (
        not candidate
        or len(candidate) > 120
        or candidate == title.strip()
        or not _CONVENTIONAL_TITLE.fullmatch(candidate)
        or any(ord(char) < 32 for char in candidate)
        or any(char in candidate for char in ("@", "<", ">", "`", "[", "]"))
        or _TITLE_URL.search(candidate)
    ):
        return None
    if mode == "when_invalid_or_inconsistent" and not (
        title_is_invalid(title, head_ref) or (check_relevance and relevance == "irrelevant")
    ):
        return None
    return candidate


def _plain_text(value: str) -> str:
    cleaned = strip_format_controls("".join(char for char in value if char == "\n" or ord(char) >= 32)).strip()
    cleaned = html.escape(cleaned, quote=False)
    cleaned = re.sub(r"([\\`*_\[\]()#!>|~])", r"\\\1", cleaned)
    cleaned = cleaned.replace("@", "\\@")
    cleaned = re.sub(r"(?i)\b([a-z][a-z0-9+.-]*)://", r"\1&#58;//", cleaned)
    cleaned = re.sub(r"(?i)\bwww\.", "www&#46;", cleaned)
    return _LINK_DOMAIN.sub(r"\1&#46;\2", cleaned)


def render_pr_description(payload: dict[str, str], *, language: str, linked_task: bool) -> str:
    headings = _HEADINGS.get(language, _HEADINGS["en"])
    fields = ("summary", "changes", "linked_task", "testing", "notes_risks")
    parts = []
    for name, heading in zip(fields, headings, strict=True):
        if not payload[name].strip() or name == "linked_task" and not linked_task:
            continue
        parts.append(f"## {heading}\n\n{_plain_text(payload[name])}")
    return "\n\n".join(parts).strip()


def render_title_relevance_note(reason: str, *, language: str) -> str:
    heading = "Проверка заголовка" if language == "ru" else "Title check"
    fallback = (
        "Заголовок PR не соответствует коммитам."
        if language == "ru"
        else "The PR title does not match the commit messages."
    )
    return f"## {heading}\n\n{_plain_text(reason or fallback)}"


def render_pr_description_comment(draft: str) -> str:
    checksum = hashlib.sha256(draft.encode("utf-8")).hexdigest()
    return f"{COMMENT_MARKER}\n<!-- autoreview-bot:pr-description-sha256:{checksum} -->\n\n{draft}\n"


def description_comment_is_intact(body: str) -> bool:
    match = _COMMENT_PATTERN.match(body)
    if match is None:
        return False
    content = body[match.end() :].rstrip("\n")
    return hashlib.sha256(content.encode("utf-8")).hexdigest() == match.group(1)


def _managed_block(draft: str) -> str:
    checksum = hashlib.sha256(draft.encode("utf-8")).hexdigest()
    return f"{_BLOCK_START}{checksum} -->\n{draft}\n{BLOCK_END}"


def _normalize_template(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def without_managed_block(body: str) -> str:
    """Remove bot-owned text before deriving links or prompts from human input."""
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    return _BOT_BLOCK.sub("", normalized)


def plan_pr_body_update(
    body: str,
    draft: str,
    *,
    mode: Literal["fill_empty", "append"],
    template: str | None = None,
) -> str | None:
    """Return a safe new body, or None when author changes must be left alone."""
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_BLOCK_PATTERN.finditer(body))
    if len(matches) == 1:
        match = matches[0]
        existing = match.group(2)
        if hashlib.sha256(existing.encode("utf-8")).hexdigest() != match.group(1):
            return None
        outside = body[: match.start()] + body[match.end() :]
        if mode == "fill_empty" and outside.strip():
            return None
        return body[: match.start()] + _managed_block(draft) + body[match.end() :]
    if matches or _BLOCK_START in body or BLOCK_END in body:
        return None

    if mode == "fill_empty":
        if body.strip() and (template is None or _normalize_template(body) != _normalize_template(template)):
            return None
        return _managed_block(draft)
    return f"{body}\n\n{_managed_block(draft)}" if body else _managed_block(draft)
