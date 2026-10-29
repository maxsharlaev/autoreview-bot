"""FastAPI entrypoint for the review launcher service."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.launcher.auth import require_launcher_auth
from app.launcher.docker_api import DockerApiError, DockerClient
from app.launcher.jobs import LauncherJobError, run_review_job
from app.launcher.paths import CheckoutPathError, validate_job_id
from app.launcher.reaper import reaper_loop
from app.launcher.timeouts import ensure_launcher_orphan_ttl_config
from app.launcher.token import launcher_token_is_configured

logger = logging.getLogger(__name__)


class RunReviewRequest(BaseModel):
    """Worker may send only these fields in the JSON body (plus auth + model key header)."""

    job_id: str = Field(min_length=1, max_length=128)
    checkout_ref: str = Field(min_length=1, max_length=512)


class RunReviewResponse(BaseModel):
    job_id: str
    status: str
    output: str
    duration_seconds: float


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not launcher_token_is_configured():
        raise RuntimeError(
            "LAUNCHER_TOKEN missing or too short (need >= 16 chars); refusing to start the review launcher"
        )
    # Orphan checkout TTL must cover max review timeout + launcher overhead.
    ensure_launcher_orphan_ttl_config()
    # Fail closed if the daemon cannot mount volume Subpaths (ARB-27-001).
    docker = DockerClient()
    try:
        docker.ping()
        docker.require_volume_subpath_support()
    except DockerApiError as exc:
        raise RuntimeError(f"Docker Engine unsuitable for review launcher: {exc}") from exc
    finally:
        docker.close()
    stop = asyncio.Event()
    task = asyncio.create_task(reaper_loop(stop), name="opr-review-reaper")
    app.state.reaper_stop = stop
    app.state.reaper_task = task
    logger.info(
        "launcher starting checkout_base=%s image=%s network=%s",
        os.environ.get("OPR_CHECKOUT_BASE", "/var/opr/checkouts"),
        os.environ.get("OPR_REVIEW_IMAGE", "opr-review:local"),
        os.environ.get("OPR_REVIEW_NETWORK", "autoreview_review_egress"),
    )
    try:
        yield
    finally:
        stop.set()
        await task


app = FastAPI(title="autoreview-bot review launcher", version="0.2.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/reviews", response_model=RunReviewResponse, dependencies=[Depends(require_launcher_auth)])
def start_review(
    body: RunReviewRequest,
    x_model_api_key: Annotated[str | None, Header(alias="X-Model-Api-Key")] = None,
) -> RunReviewResponse:
    """Start a hard-coded review container.

    Body is only ``job_id`` + ``checkout_ref``. The model key is passed in
    ``X-Model-Api-Key`` for M2 (removed once M3 model-API proxy lands). Job
    tunables (model, timeout, sandbox) are read from staged ``job.json``.
    """
    try:
        validate_job_id(body.job_id)
    except CheckoutPathError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    key = (x_model_api_key or "").strip()
    if not key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Model-Api-Key required")
    try:
        result = run_review_job(
            job_id=body.job_id,
            checkout_ref=body.checkout_ref,
            model_api_key=key,
        )
    except LauncherJobError as exc:
        # Map all launcher/container failures to a fail-closed sandbox signal for the worker.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return RunReviewResponse(
        job_id=result["job_id"],
        status=result["status"],
        output=result["output"],
        duration_seconds=result["duration_seconds"],
    )
