"""Optional static admin control UI (Next.js export) under ``ADMIN_UI_BASE_PATH``."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from app.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_UI_DIST = "admin_ui_out"
DEFAULT_ADMIN_UI_BASE_PATH = "/control"


class _ControlStaticFiles(StaticFiles):
    """Serve the export directory; unknown paths fall back to index.html."""

    async def get_response(self, path: str, scope: dict) -> Response:  # type: ignore[override]
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)


def resolve_admin_ui_dist(settings: Settings) -> Path:
    raw = (settings.admin_ui_dist or DEFAULT_ADMIN_UI_DIST).strip() or DEFAULT_ADMIN_UI_DIST
    return Path(raw).expanduser()


def admin_ui_base_path(settings: Settings) -> str:
    """Normalized mount path (validated on Settings load)."""
    return (settings.admin_ui_base_path or DEFAULT_ADMIN_UI_BASE_PATH).rstrip("/") or DEFAULT_ADMIN_UI_BASE_PATH


def mount_admin_ui(application: FastAPI, settings: Settings) -> None:
    """Mount the control UI when enabled and the export directory contains ``index.html``."""
    if settings.admin_ui_enabled is not True:
        return
    root = resolve_admin_ui_dist(settings).resolve()
    if not (root / "index.html").is_file():
        raise RuntimeError(
            f"ADMIN_UI_ENABLED=true but admin UI export is missing or incomplete at {root} "
            "(expected index.html). Build admin-ui (npm ci && npm run build) and copy out/ "
            "to that path, or set ADMIN_UI_DIST."
        )
    mount = admin_ui_base_path(settings)
    application.mount(mount, _ControlStaticFiles(directory=str(root), html=True), name="admin-ui")
    logger.info("Admin UI mounted at %s (dist=%s)", mount, root)
