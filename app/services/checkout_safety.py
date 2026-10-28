"""Neutralize escaping symlinks in PR checkouts (ARB-27-002).

PR checkouts are untrusted. A symlink to an absolute path (e.g. /proc/self/environ)
or a path that resolves outside the repo root must never be followed by the worker
when staging or reading checkout trees — that would leak secrets held in the
worker process into staged files or model context.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def symlink_target_escapes(root: Path, link_path: Path, target: str) -> bool:
    """Return True if symlink ``target`` from ``link_path`` escapes ``root``.

    Checks (1) absolute targets, (2) lexical normalization of the join, and
    (3) resolved path relative to ``root`` (without allowing an outside final path).
    """
    root_resolved = root.resolve()
    if not target:
        return True
    # Absolute (POSIX or Windows drive/UNC) — never allow.
    if os.path.isabs(target):
        return True
    # Lexical join + normpath (does not follow the link).
    lexical = Path(os.path.normpath(str(link_path.parent / target)))
    try:
        lexical.relative_to(root_resolved)
    except ValueError:
        return True
    # Resolve following symlinks; final path must stay under root.
    try:
        resolved = link_path.resolve()
    except OSError:
        return True
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return True
    return False


def neutralize_escaping_symlinks(root: Path) -> int:
    """Remove absolute / outside-resolving symlinks under ``root``.

    Relative in-repo symlinks are kept. Directory symlinks are never descended
    into (whether kept or removed). Returns the number of removed links.
    """
    root_resolved = root.resolve()
    removed = 0
    if not root_resolved.is_dir():
        return 0
    for dirpath, dirnames, filenames in os.walk(root_resolved, topdown=True, followlinks=False):
        # Never descend into any symlink directory.
        for name in list(dirnames):
            path = Path(dirpath) / name
            if path.is_symlink():
                dirnames.remove(name)
                try:
                    target = os.readlink(path)
                except OSError:
                    path.unlink(missing_ok=True)
                    removed += 1
                    continue
                if symlink_target_escapes(root_resolved, path, target):
                    path.unlink(missing_ok=True)
                    removed += 1
                    logger.info("removed escaping dir symlink %s -> %s", path, target)
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                continue
            try:
                target = os.readlink(path)
            except OSError:
                path.unlink(missing_ok=True)
                removed += 1
                continue
            if symlink_target_escapes(root_resolved, path, target):
                path.unlink(missing_ok=True)
                removed += 1
                logger.info("removed escaping symlink %s -> %s", path, target)
    return removed


def safe_copytree(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` without dereferencing symlinks, then neutralize escapes.

    Uses ``symlinks=True`` so attacker links are not expanded into file content
    (which would leak worker secrets such as /proc/self/environ). Escaping links
    are removed from the destination; relative in-repo links are preserved.
    """
    source = src.resolve()
    if not source.is_dir():
        raise NotADirectoryError(f"checkout source is not a directory: {source}")
    shutil.copytree(source, dst, symlinks=True, dirs_exist_ok=False)
    neutralize_escaping_symlinks(dst)
