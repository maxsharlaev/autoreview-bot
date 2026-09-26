from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from app.adapters.github import ChangedFile
from app.config import CodexYaml, LanguageYaml
from app.paths import data_file
from app.services.constants import PROMPT_VERSION, SKIP_LOCKFILES, SKIP_SUFFIXES, SKIP_TOO_LARGE


@dataclass
class PreviousFinding:
    stable_id: str
    severity: str
    title: str
    path: str
    line: int | None
    status: str
    evidence: str
    scenario: str = ""
    recommendation: str = ""


@dataclass
class ReviewContext:
    prompt: str
    prompt_version: str
    truncated: bool
    skip_reason: str | None
    files_total: int
    files_reviewed: int
    diff_lines: int
    skipped_paths: list[str] = field(default_factory=list)
    changed_paths: set[str] = field(default_factory=set)


def _is_skipped(path: str) -> bool:
    name = Path(path).name
    if name in SKIP_LOCKFILES:
        return True
    if name.startswith(".env"):
        return True
    suffix = Path(path).suffix.lower()
    return suffix in SKIP_SUFFIXES


def _clip(text: str, limit: int = 800) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _wrap(tag: str, value: str) -> str:
    cleaned = (value or "").replace(f"</{tag}>", "")
    return f"<{tag}>\n{cleaned}\n</{tag}>"


def build_context(
    *,
    repository: str,
    pr_number: int,
    title: str,
    body: str,
    head_ref: str,
    base_sha: str,
    head_sha: str,
    previous_head_sha: str | None,
    files: list[ChangedFile],
    issue_key: str | None,
    jira_text: str | None,
    jira_warning: str | None,
    previous_findings: list[PreviousFinding],
    limits: CodexYaml,
    language: LanguageYaml | None = None,
) -> ReviewContext:
    skipped: list[str] = []
    reviewed: list[ChangedFile] = []
    diff_lines = 0
    for item in files:
        if _is_skipped(item.path):
            skipped.append(item.path)
            continue
        lines = item.patch.count("\n") + (1 if item.patch else 0)
        diff_lines += lines
        reviewed.append(item)

    skip_reason = None
    truncated = False
    if len(files) > limits.max_files or diff_lines > limits.max_diff_lines:
        truncated = True
        skip_reason = SKIP_TOO_LARGE
        skipped = [item.path for item in files]
        reviewed = []

    custom_prompt = limits.prompt_file.strip()
    prompt_path = Path(custom_prompt).expanduser() if custom_prompt else data_file("prompts", "review.md")
    if custom_prompt and not prompt_path.is_file():
        raise FileNotFoundError(f"Review prompt file does not exist: {prompt_path}")
    locale = language or LanguageYaml()
    language_names = {"en": "English", "ru": "Russian"}
    instructions = (
        prompt_path.read_text(encoding="utf-8")
        .replace("{{summary_language}}", language_names[locale.summary])
        .replace("{{details_language}}", language_names[locale.details])
    )
    prompt_version = (
        f"custom-{hashlib.sha256(instructions.encode('utf-8')).hexdigest()[:16]}" if custom_prompt else PROMPT_VERSION
    )

    file_blocks = []
    for item in reviewed:
        patch = item.patch
        if len(patch.encode("utf-8")) > limits.max_file_bytes:
            patch = patch.encode("utf-8")[: limits.max_file_bytes].decode("utf-8", errors="ignore")
            patch += "\n[truncated]"
        file_blocks.append(
            _wrap(
                "untrusted_file_diff",
                f"path: {item.path}\nstatus: {item.status}\n{patch}",
            )
        )

    previous_block = "none"
    if previous_findings:
        rows = []
        for item in previous_findings:
            loc = f"{item.path}:{item.line}" if item.line else item.path
            rows.append(
                "\n".join(
                    [
                        f"- id={item.stable_id} severity={item.severity} status={item.status} path={loc}",
                        f"  title: {item.title}",
                        f"  scenario: {_clip(item.scenario)}",
                        f"  evidence: {_clip(item.evidence)}",
                        f"  recommendation: {_clip(item.recommendation)}",
                    ]
                )
            )
        previous_block = _wrap("untrusted_previous_findings", "\n".join(rows))

    jira_block = jira_warning or "not provided"
    if jira_text:
        jira_block = _wrap("untrusted_jira_issue", jira_text)

    prompt = "\n\n".join(
        [
            instructions,
            f"repository: {repository}",
            f"pull_request_number: {pr_number}",
            f"base_sha: {base_sha}",
            f"head_sha: {head_sha}",
            f"previous_head_sha: {previous_head_sha or 'null'}",
            f"issue_key: {issue_key or 'null'}",
            _wrap("untrusted_pr_title", title),
            _wrap("untrusted_pr_body", body),
            _wrap("untrusted_branch", head_ref),
            f"jira_context:\n{jira_block}",
            f"previous_review_state:\n{previous_block}",
            "changed_files:\n" + "\n".join(file_blocks),
            f"skipped_paths: {', '.join(skipped) if skipped else 'none'}",
            f"truncated: {str(truncated).lower()}",
        ]
    )
    return ReviewContext(
        prompt=prompt,
        prompt_version=prompt_version,
        truncated=truncated,
        skip_reason=skip_reason,
        files_total=len(files),
        files_reviewed=len(reviewed),
        diff_lines=diff_lines,
        skipped_paths=skipped,
        changed_paths={item.path for item in reviewed},
    )
