from app.api.v1 import api_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_health() -> None:
    application = FastAPI()
    application.include_router(api_router)
    client = TestClient(application)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
