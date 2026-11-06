from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from common.context import Context
from common.errors import DomainError
from common.store import MemoryStore
from common.time import iso
from services.bookings.main import build_app


@pytest.fixture

def client_setup():
    now = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
    hold_id = str(uuid4())
    calls = []
    def request(service, method, path, json=None):
        calls.append((service, method, path, json))
        if service == "pricing":
            return {"amount": 50000, "currency": "RUB"}
        if service == "schedule":
            return {"id": hold_id, "expires_at": iso(now + timedelta(minutes=10))}
        return {"id": str(uuid4())}
    ctx = Context("bookings", MemoryStore("bookings"), SimpleNamespace(request=request), now=lambda: now)
    payload = {"user_id": str(uuid4()), "table_id": str(uuid4()), "quote_id": str(uuid4()),
               "start_at": iso(now + timedelta(hours=3)), "end_at": iso(now + timedelta(hours=4))}
    with TestClient(build_app(ctx, background=False)) as client:
        yield client, ctx, payload, calls


def test_http_create_read_filter_and_cancel(client_setup):
    client, ctx, payload, calls = client_setup
    response = client.post("/api/bookings", json=payload)
    assert response.status_code == 201
    booking = response.json()
    assert booking["status"] == "waiting_payment"
    assert [call[0] for call in calls] == ["users", "pricing", "schedule"]
    assert client.get(f"/api/bookings/{booking['id']}").json()["amount"] == 50000
    assert len(client.get("/api/bookings", params={"user_id": payload["user_id"]}).json()) == 1
    assert client.get("/api/bookings", params={"user_id": str(uuid4())}).json() == []
    assert client.post(f"/api/bookings/{booking['id']}:cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/bookings/{booking['id']}:cancel").status_code == 200


def test_http_request_validation_and_not_found(client_setup):
    client, _, payload, _ = client_setup
    assert client.post("/api/bookings", json={**payload, "user_id": "broken"}).status_code == 422
    assert client.post("/api/bookings", json={**payload, "start_at": "2030-01-01T13:00:00"}).status_code == 422
    assert client.get(f"/api/bookings/{uuid4()}").status_code == 404
    assert client.get("/api/bookings", params={"limit": 0}).status_code == 422


def test_downstream_failure_is_reported_without_partial_local_booking(client_setup):
    client, ctx, payload, _ = client_setup
    def fail(*args, **kwargs):
        raise DomainError(503, "Тарифы недоступны")
    ctx.http.request = fail
    response = client.post("/api/bookings", json=payload)
    assert response.status_code == 503
    assert client.get("/api/bookings").json() == []
