"""Optional static admin control UI (Next.js export) under ``/control``."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.config import Settings

logger = logging.getLogger(__name__)

CONTROL_MOUNT = "/control"
DEFAULT_ADMIN_UI_DIST = "admin_ui_out"


class _ControlStaticFiles(StaticFiles):
    """Serve the export directory; unknown paths fall back to index.html."""

    async def get_response(self, path: str, scope: dict) -> Response:  # type: ignore[override]
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)


def resolve_admin_ui_dist(settings: Settings) -> Path:
    raw = (settings.admin_ui_dist or DEFAULT_ADMIN_UI_DIST).strip() or DEFAULT_ADMIN_UI_DIST
    return Path(raw).expanduser()


def mount_admin_ui(application: FastAPI, settings: Settings) -> None:
    """Mount ``/control`` when enabled and the export directory contains ``index.html``."""
    if settings.admin_ui_enabled is not True:
        return
    root = resolve_admin_ui_dist(settings).resolve()
    if not (root / "index.html").is_file():
        raise RuntimeError(
            f"ADMIN_UI_ENABLED=true but admin UI export is missing or incomplete at {root} "
            "(expected index.html). Build admin-ui (npm ci && npm run build) and copy out/ "
            "to that path, or set ADMIN_UI_DIST."
        )
    application.mount(CONTROL_MOUNT, _ControlStaticFiles(directory=str(root), html=True), name="admin-ui")
    logger.info("Admin UI mounted at %s (dist=%s)", CONTROL_MOUNT, root)
