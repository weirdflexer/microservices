from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from common.context import Context
from common.messaging import process_incoming
from common.store import MemoryStore
from common.time import iso
from services.sessions.main import build_app


@pytest.fixture

def client_setup():
    now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    data = {"booking_id": str(uuid4()), "user_id": str(uuid4()), "table_id": str(uuid4()),
            "start_at": iso(now), "end_at": iso(now + timedelta(hours=2))}
    calls = []
    def request(service, method, path, json=None):
        calls.append((service, method, path))
        return {**data, "status": "confirmed", "start_at": "2030-01-01T12:00:00Z"}
    ctx = Context("sessions", MemoryStore("sessions"), SimpleNamespace(request=request), now=lambda: now)
    app = build_app(ctx, background=False)
    with TestClient(app) as client:
        process_incoming(app.state.service, ctx, {"id": str(uuid4()), "occurred_at": iso(now),
                         "type": "booking.confirmed", "producer": "bookings", "correlation_id": data["booking_id"], "data": data})
        session = client.get("/api/sessions").json()[0]
        yield client, ctx, session, calls


def test_http_start_and_complete_use_booking_contract(client_setup):
    client, ctx, session, calls = client_setup
    assert client.get(f"/api/sessions/{session['id']}").json()["status"] == "expected"
    response = client.post(f"/api/sessions/{session['id']}:start")
    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert calls[0][0] == "bookings"
    assert client.post(f"/api/sessions/{session['id']}:complete").json()["status"] == "completed"
    assert ctx.store.snapshot()["outbox"][0]["type"] == "session.completed"


def test_http_rejects_completion_before_start_and_missing_visit(client_setup):
    client, _, session, _ = client_setup
    assert client.post(f"/api/sessions/{session['id']}:complete").status_code == 409
    assert client.get(f"/api/sessions/{uuid4()}").status_code == 404
    assert client.get("/api/sessions/not-a-uuid").status_code == 422


def test_http_too_late_start_is_conflict(client_setup):
    client, ctx, session, calls = client_setup
    ctx.now = lambda: datetime(2030, 1, 1, 12, 15, tzinfo=timezone.utc)
    assert client.post(f"/api/sessions/{session['id']}:start").status_code == 409
    assert calls == []
