"""Conservative repository visibility classification for publication policy."""

from __future__ import annotations

from typing import Any, Literal

Visibility = Literal["private", "public"]


def repository_visibility(data: dict[str, Any] | None) -> Visibility | None:
    if not isinstance(data, dict):
        return None
    private = data.get("private")
    named = data.get("visibility")
    if private is False or named == "public" or named == "internal":
        return "public"
    if private is True and (named is None or named == "private"):
        return "private"
    if named == "private" and private is None:
        return "private"
    return None


def confirmed_visibility(webhook: Visibility | None, api: Visibility | None) -> Visibility:
    if api == "private" and webhook in {None, "private"}:
        return "private"
    return "public"
