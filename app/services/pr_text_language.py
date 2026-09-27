"""Resolve and check the language of generated PR text."""

from __future__ import annotations

import re

from app.config import AppConfig
from app.services.issue_key import ISSUE_KEY_RE

_CONVENTIONAL_PREFIX = re.compile(r"^[a-z][a-z0-9-]*(?:\([^)]*\))?!?:\s*", re.IGNORECASE)


def _detect(text: str) -> str | None:
    text = ISSUE_KEY_RE.sub("", text)
    text = _CONVENTIONAL_PREFIX.sub("", text)
    letters = [char for char in text if char.isalpha()]
    cyrillic = sum("\u0400" <= char <= "\u052f" for char in letters)
    latin = sum("a" <= char.lower() <= "z" for char in letters)
    if cyrillic >= 4 and cyrillic > latin:
        return "ru"
    if latin >= 5 and latin > cyrillic:
        return "en"
    return None


def resolve_pr_language(config: AppConfig, *, human_title: str, human_body: str, commits: list[str]) -> str:
    configured = config.pr_description.language or config.pr_text.language or config.language.details
    if configured != "auto":
        return configured
    for text in (human_title, human_body, "\n".join(_CONVENTIONAL_PREFIX.sub("", item) for item in commits)):
        detected = _detect(text)
        if detected is not None:
            return detected
    return config.language.details


def output_language_matches(payload: dict[str, str], language: str) -> bool:
    if payload["output_language"].lower() != language:
        return False
    if language not in {"en", "ru"}:
        return True
    for name in ("summary", "changes", "testing", "notes_risks"):
        detected = _detect(payload[name])
        if detected is not None and detected != language:
            return False
    subject = _CONVENTIONAL_PREFIX.sub("", payload["suggested_title"])
    title_detected = _detect(subject)
    return title_detected is None or title_detected == language
