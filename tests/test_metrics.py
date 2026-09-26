import json

from app.api.v1 import api_router
from app.logging_setup import JsonLogFormatter
from app.metrics import _hints, record_codex_attempt, record_enqueue, record_review_finished
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_metrics_endpoint_without_backend() -> None:
    application = FastAPI()
    application.include_router(api_router)
    client = TestClient(application)
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert "open_pr_review_review_runs_total" in body or "python_info" in body


def test_ops_status_requires_key() -> None:
    application = FastAPI()
    application.include_router(api_router)
    client = TestClient(application)
    response = client.get("/api/v1/ops/status")
    assert response.status_code == 401


def test_record_review_finished_emits_counter() -> None:
    record_review_finished(status="failed", error_code="CODEX_QUOTA", trigger="manual", duration_ms=1200)
    record_codex_attempt(result="CODEX_QUOTA", duration_s=1.2)
    record_enqueue("queued")


def test_quota_hint() -> None:
    class Run:
        error_code = "CODEX_QUOTA"

    hints = _hints({"pending": 0, "running": 0}, [], [Run()])
    assert any("quota" in item.lower() for item in hints)


def test_queue_not_running_hint() -> None:
    hints = _hints({"pending": 3, "running": 0}, [], [])
    assert any("not consuming" in item.lower() or "queued" in item.lower() for item in hints)


def test_json_log_includes_service_and_run() -> None:
    import logging

    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="open_pr_review.review",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="[aa review] failed: CODEX_QUOTA",
        args=(),
        exc_info=None,
    )
    record.review_run_id = "abc"
    record.error_code = "CODEX_QUOTA"
    payload = json.loads(formatter.format(record))
    assert payload["msg"].endswith("CODEX_QUOTA")
    assert payload["review_run_id"] == "abc"
    assert payload["error_code"] == "CODEX_QUOTA"
    assert "ts" in payload
