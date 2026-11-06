from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from common.context import Context
from common.messaging import process_incoming
from common.store import MemoryStore
from common.time import iso
from services.payments.main import build_app


@pytest.fixture

def client_setup():
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    ctx = Context("payments", MemoryStore("payments"), SimpleNamespace(), now=lambda: now)
    app = build_app(ctx, background=False)
    event = {"id": str(uuid4()), "type": "booking.payment_requested", "occurred_at": iso(now),
             "producer": "bookings", "correlation_id": str(uuid4()), "data": {
                 "booking_id": str(uuid4()), "user_id": str(uuid4()), "amount": 50000, "currency": "RUB"}}
    with TestClient(app) as client:
        process_incoming(app.state.service, ctx, event)
        payment = client.get("/api/payments").json()[0]
        yield client, app, ctx, payment


def test_provider_callback_authentication_and_idempotency(client_setup):
    client, app, ctx, payment = client_setup
    path = f"/api/payments/{payment['id']}:result"
    payload = {"status": "succeeded", "amount": 50000, "currency": "RUB"}
    assert client.post(path, json=payload).status_code == 401
    assert client.post(path, json=payload, headers={"X-Provider-Token": "wrong"}).status_code == 401
    headers = {"X-Provider-Token": "demo-provider-token"}
    assert client.post(path, json=payload, headers=headers).json()["status"] == "succeeded"
    assert client.post(path, json=payload, headers=headers).status_code == 200
    assert sum(event["type"] == "payment.succeeded" for event in ctx.store.snapshot()["outbox"]) == 1
    assert client.get(f"/api/payments/{payment['id']}").json()["status"] == "succeeded"


def test_wrong_amount_or_shape_does_not_record_success(client_setup):
    client, _, _, payment = client_setup
    path = f"/api/payments/{payment['id']}:result"
    headers = {"X-Provider-Token": "demo-provider-token"}
    assert client.post(path, json={"status": "succeeded", "amount": 1, "currency": "RUB"}, headers=headers).status_code == 409
    assert client.post(path, json={"status": "succeeded", "amount": "50000", "currency": "RUB"}, headers=headers).status_code == 422
    assert client.get(f"/api/payments/{uuid4()}").status_code == 404


def test_refund_http_callback_after_async_request(client_setup):
    client, app, ctx, payment = client_setup
    headers = {"X-Provider-Token": "demo-provider-token"}
    client.post(f"/api/payments/{payment['id']}:result", json={
        "status": "succeeded", "amount": 50000, "currency": "RUB"}, headers=headers)
    process_incoming(app.state.service, ctx, {"id": str(uuid4()), "occurred_at": iso(ctx.now()),
        "type": "booking.refund_requested", "producer": "bookings", "correlation_id": payment["booking_id"], "data": {"payment_id": payment["id"],
        "booking_id": payment["booking_id"], "user_id": payment["user_id"], "amount": 50000,
        "currency": "RUB", "reason": "cancelled"}})
    response = client.post(f"/api/payments/{payment['id']}:refund-result", json={"status": "succeeded"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["refunded_amount"] == 50000
