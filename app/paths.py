"""Locate repo data files after pip install (Docker) and in a source checkout."""

from __future__ import annotations

import os
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_SOURCE_ROOT = _APP_DIR.parent


def data_file(*parts: str) -> Path:
    rel = Path(*parts)
    configured = os.environ.get("OPEN_PR_REVIEW_DATA_DIR", "").strip()
    bases = []
    if configured:
        bases.append(Path(configured))
    bases.extend(
        [
            Path("/app"),
            Path.cwd(),
            _SOURCE_ROOT,
            _APP_DIR,
        ]
    )
    seen: set[Path] = set()
    tried: list[str] = []
    for base in bases:
        path = (base / rel).resolve()
        if path in seen:
            continue
        seen.add(path)
        tried.append(str(path))
        if path.is_file():
            return path
    raise FileNotFoundError(f"Missing {'/'.join(parts)}; looked in: {', '.join(tried)}")
