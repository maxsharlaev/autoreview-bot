from __future__ import annotations

import re

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,10}-\d{1,6})\b")


def extract_issue_key(*parts: str | None) -> str | None:
    for part in parts:
        if not part:
            continue
        match = ISSUE_KEY_RE.search(part)
        if match:
            return match.group(1)
    return None


def remove_issue_key(text: str, issue_key: str | None) -> str:
    """Remove only the linked Jira key, preserving unrelated technical terms."""
    if not issue_key:
        return text
    return re.sub(rf"\b{re.escape(issue_key)}\b", "", text)
