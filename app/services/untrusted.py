"""Keep untrusted prompt data from imitating trust-boundary delimiters."""

from __future__ import annotations

import re

_BOUNDARY_TAG = re.compile(r"<\s*/?\s*untrusted(?:_[a-z0-9_]+)?\s*>", re.IGNORECASE)


def strip_boundary_tags(value: str) -> str:
    previous = value
    for _ in range(64):
        cleaned = _BOUNDARY_TAG.sub("", previous)
        if cleaned == previous:
            return cleaned
        previous = cleaned
    return previous.replace("<", "&lt;")
