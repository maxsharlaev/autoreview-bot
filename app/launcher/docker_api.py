"""Thin Docker Engine API client over the unix socket / named pipe.

Uses httpx + a custom transport so unit tests can inject a mock transport without
the ``docker`` PyPI package. Production talks to ``DOCKER_HOST`` or the default
socket path.

Cancel-path calls pass a wall-clock ``timeout`` (remaining cancel budget). That
bound is enforced with ``asyncio.timeout`` around an ``httpx.AsyncClient``
request (including body read), not HTTPX per-phase inactivity timeouts alone —
trickled responses cannot outlive the absolute deadline.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import time
from typing import Any

import httpx

from app.launcher.timeouts import DOCKER_WAIT_CLIENT_GRACE_SECONDS

logger = logging.getLogger(__name__)

DEFAULT_UNIX_SOCKET = "unix:///var/run/docker.sock"


class DockerApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class DockerDeadlineExceeded(DockerApiError):
    """Absolute wall-clock deadline for a cancel-path Docker call was hit."""


def docker_base_url() -> str:
    host = os.environ.get("DOCKER_HOST", "").strip()
    if host:
        return host
    return DEFAULT_UNIX_SOCKET


def _make_client(timeout: float = 60.0) -> httpx.Client:
    base = docker_base_url()
    if base.startswith("unix://"):
        socket_path = base.removeprefix("unix://")
        transport = httpx.HTTPTransport(uds=socket_path)
        # Docker Engine API expects Host and a dummy http base URL with uds transport.
        return httpx.Client(transport=transport, base_url="http://docker", timeout=timeout)
    if base.startswith("npipe:"):
        raise DockerApiError("named-pipe DOCKER_HOST is not supported in-launcher; run launcher in Linux")
    return httpx.Client(base_url=base, timeout=timeout)


def _make_async_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    base = docker_base_url()
    if base.startswith("unix://"):
        socket_path = base.removeprefix("unix://")
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=timeout)
    if base.startswith("npipe:"):
        raise DockerApiError("named-pipe DOCKER_HOST is not supported in-launcher; run launcher in Linux")
    return httpx.AsyncClient(base_url=base, timeout=timeout)


def _run_coro_sync(coro: Any) -> Any:
    """Run ``coro`` from sync launcher code.

    Cancel endpoints are sync FastAPI handlers (threadpool, no running loop).
    If a loop is already running, run in a fresh thread with its own loop so we
    never nest ``asyncio.run`` or leave work on a cancelled task.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _arequest_with_wall_deadline(
    method: str,
    path: str,
    *,
    wall_timeout: float,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> httpx.Response:
    """HTTP request whose *entire* lifetime (connect + headers + body) is capped.

    ``asyncio.timeout`` cancels the awaitable and closes the AsyncClient, so the
    underlying connection does not keep draining a trickling response after the
    cancel budget is gone. HTTPX inactivity timeouts are set to the same budget
    only as a secondary backstop.
    """
    if wall_timeout <= 0:
        raise DockerDeadlineExceeded("docker call deadline exceeded before start")
    # Tiny floor so we do not pass 0 to HTTPX / asyncio.timeout.
    budget = max(0.001, float(wall_timeout))
    started = time.monotonic()
    timeout = httpx.Timeout(budget)
    client = _make_async_client(timeout)
    try:
        async with asyncio.timeout(budget):
            response = await client.request(method, path, params=params, json=json_body)
            # Non-stream request already buffers the body; aread is cheap / no-op
            # when content is cached, and keeps streamed callers honest.
            await response.aread()
            return response
    except TimeoutError as exc:
        elapsed = time.monotonic() - started
        raise DockerDeadlineExceeded(
            f"docker call wall-clock deadline exceeded after {elapsed:.3f}s (budget={budget:.3f}s)"
        ) from exc
    except httpx.TimeoutException as exc:
        # Inactivity timeout fired inside the wall budget (e.g. stalled connect).
        elapsed = time.monotonic() - started
        raise DockerDeadlineExceeded(
            f"docker call timed out after {elapsed:.3f}s (budget={budget:.3f}s): {exc}"
        ) from exc
    finally:
        await client.aclose()


class DockerClient:
    """Subset of Docker Engine API used by the launcher."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self._owns = client is None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = _make_client()
        return self._client

    def close(self) -> None:
        if self._owns and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> DockerClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _deadline_request(
        self,
        method: str,
        path: str,
        *,
        wall_timeout: float,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        """Cancel-path request with a reliable absolute deadline."""
        return _run_coro_sync(
            _arequest_with_wall_deadline(
                method,
                path,
                wall_timeout=wall_timeout,
                params=params,
                json_body=json_body,
            )
        )

    def ping(self) -> None:
        r = self._http().get("/_ping")
        if r.status_code != 200:
            raise DockerApiError("docker ping failed", status_code=r.status_code, body=r.text)

    def version(self) -> dict[str, Any]:
        r = self._http().get("/version")
        if r.status_code != 200:
            raise DockerApiError("docker version failed", status_code=r.status_code, body=r.text[:500])
        data = r.json()
        if not isinstance(data, dict):
            raise DockerApiError("docker version returned non-object")
        return data

    def api_version_tuple(self) -> tuple[int, ...]:
        data = self.version()
        raw = str(data.get("ApiVersion") or data.get("apiVersion") or "").strip()
        if not raw:
            raise DockerApiError("docker version missing ApiVersion")
        parts: list[int] = []
        for piece in raw.split("."):
            if not piece.isdigit():
                raise DockerApiError(f"unparseable Docker ApiVersion: {raw!r}")
            parts.append(int(piece))
        return tuple(parts)

    def require_volume_subpath_support(self) -> None:
        """Fail closed unless Engine API supports volume Mount Subpath (API >= 1.45 / Engine 26+)."""
        ver = self.api_version_tuple()
        if ver < (1, 45):
            raise DockerApiError(
                f"Docker Engine API {'.'.join(str(x) for x in ver)} is too old; "
                "need ApiVersion >= 1.45 (Engine 26+) for volume Subpath mounts"
            )

    def create_container(self, name: str, config: dict[str, Any]) -> str:
        r = self._http().post("/containers/create", params={"name": name}, json=config)
        if r.status_code not in (200, 201):
            raise DockerApiError(
                f"container create failed: {r.status_code}",
                status_code=r.status_code,
                body=r.text[:500],
            )
        data = r.json()
        cid = data.get("Id")
        if not cid:
            raise DockerApiError("container create returned no Id", body=r.text[:200])
        return str(cid)

    def start_container(self, container_id: str) -> None:
        r = self._http().post(f"/containers/{container_id}/start")
        if r.status_code not in (204, 304):
            raise DockerApiError(
                f"container start failed: {r.status_code}",
                status_code=r.status_code,
                body=r.text[:500],
            )

    def wait_container(self, container_id: str, *, timeout_seconds: int) -> int:
        # Docker wait blocks until exit; client timeout = job timeout + grace.
        r = self._http().post(
            f"/containers/{container_id}/wait",
            timeout=float(timeout_seconds) + float(DOCKER_WAIT_CLIENT_GRACE_SECONDS),
        )
        if r.status_code != 200:
            raise DockerApiError(
                f"container wait failed: {r.status_code}",
                status_code=r.status_code,
                body=r.text[:500],
            )
        data = r.json()
        return int(data.get("StatusCode", 1))

    def logs(self, container_id: str, *, tail: int = 200) -> str:
        r = self._http().get(
            f"/containers/{container_id}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(tail)},
        )
        if r.status_code != 200:
            return ""
        # Raw multiplexed stream; decode lossily for diagnostics (no secrets expected in logs).
        return r.content.decode("utf-8", errors="replace")[-4000:]

    def remove_container(self, container_id: str, *, force: bool = True, timeout: float | None = None) -> None:
        params = {"force": "true" if force else "false", "v": "true"}
        if timeout is not None:
            r = self._deadline_request(
                "DELETE",
                f"/containers/{container_id}",
                wall_timeout=float(timeout),
                params=params,
            )
        else:
            r = self._http().delete(f"/containers/{container_id}", params=params)
        if r.status_code not in (204, 404):
            logger.warning("container remove %s failed: %s %s", container_id[:12], r.status_code, r.text[:200])

    def list_review_containers(self) -> list[dict[str, Any]]:
        from app.launcher.spec import REVIEW_LABEL, REVIEW_LABEL_VALUE

        r = self._http().get(
            "/containers/json",
            params={"all": "true", "filters": json.dumps({"label": [f"{REVIEW_LABEL}={REVIEW_LABEL_VALUE}"]})},
        )
        if r.status_code != 200:
            raise DockerApiError(
                f"container list failed: {r.status_code}",
                status_code=r.status_code,
                body=r.text[:500],
            )
        return list(r.json())

    def list_containers_for_job(self, job_id: str, *, timeout: float | None = None) -> list[dict[str, Any]]:
        """Containers labeled ``opr.job_id=<job_id>`` (any state)."""
        params = {"all": "true", "filters": json.dumps({"label": [f"opr.job_id={job_id}"]})}
        if timeout is not None:
            r = self._deadline_request("GET", "/containers/json", wall_timeout=float(timeout), params=params)
        else:
            r = self._http().get("/containers/json", params=params)
        if r.status_code != 200:
            raise DockerApiError(
                f"container list by job failed: {r.status_code}",
                status_code=r.status_code,
                body=r.text[:500],
            )
        return list(r.json())

    def kill_container(self, container_id: str, *, timeout: float | None = None) -> None:
        params = {"signal": "KILL"}
        if timeout is not None:
            r = self._deadline_request(
                "POST",
                f"/containers/{container_id}/kill",
                wall_timeout=float(timeout),
                params=params,
            )
        else:
            r = self._http().post(f"/containers/{container_id}/kill", params=params)
        if r.status_code not in (204, 404):
            logger.warning("container kill %s: %s", container_id[:12], r.status_code)
