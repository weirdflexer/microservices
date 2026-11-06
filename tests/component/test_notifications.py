from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from common.context import Context
from common.store import MemoryStore
from services.notifications.main import build_app


@pytest.fixture

def client_setup():
    calls = []
    def request(service, method, path, json=None):
        calls.append((service, method, path))
        return {"email": "student@example.test"}
    ctx = Context("notifications", MemoryStore("notifications"), SimpleNamespace(request=request),
                  now=lambda: datetime(2030, 1, 1, 12, tzinfo=timezone.utc))
    with TestClient(build_app(ctx, background=False)) as client:
        yield client, ctx, calls


def test_http_create_send_get_and_filter(client_setup):
    client, _, calls = client_setup
    user_id = str(uuid4())
    response = client.post("/api/notifications", json={"user_id": user_id, "kind": "custom", "message": "Тест"})
    assert response.status_code == 201
    notification_id = response.json()["id"]
    sent = client.post(f"/api/notifications/{notification_id}:send")
    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"
    assert calls == [("users", "GET", f"/api/users/{user_id}")]
    assert client.get(f"/api/notifications/{notification_id}").json()["attempts"] == 1
    assert len(client.get("/api/notifications", params={"user_id": user_id}).json()) == 1
    assert client.get("/api/notifications", params={"user_id": str(uuid4())}).json() == []


def test_http_validation_missing_and_future_sending(client_setup):
    client, _, calls = client_setup
    user_id = str(uuid4())
    assert client.post("/api/notifications", json={"user_id": user_id, "message": ""}).status_code == 422
    assert client.post("/api/notifications", json={"user_id": user_id, "message": "  "}).status_code == 422
    assert client.get(f"/api/notifications/{uuid4()}").status_code == 404
    notification = client.post("/api/notifications", json={"user_id": user_id, "message": "Позже",
                                      "scheduled_at": "2030-01-02T12:00:00Z"}).json()
    assert client.post(f"/api/notifications/{notification['id']}:send").status_code == 409
    assert calls == []
