from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from common.errors import DomainError
from common.store import MemoryStore
from services.payments import Service


@pytest.fixture

def setup():
    ctx = SimpleNamespace(store=MemoryStore("payments"), now=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    service = Service(ctx)
    data = {"booking_id": str(uuid4()), "user_id": str(uuid4()), "amount": 50000, "currency": "RUB"}
    with ctx.store.transaction() as tx:
        service.handle({"type": "booking.payment_requested", "data": data}, tx)
    return service, ctx, data, service.list()[0]


def test_payment_request_is_unique_per_booking(setup):
    service, ctx, data, _ = setup
    with ctx.store.transaction() as tx:
        service.handle({"type": "booking.payment_requested", "data": data}, tx)
    assert len(service.list()) == 1
    assert len(ctx.store.snapshot()["outbox"]) == 1


def test_success_callback_idempotent_and_cannot_be_demoted(setup):
    service, ctx, _, payment = setup
    payload = {"status": "succeeded", "amount": payment["amount"], "currency": "RUB"}
    service.result(payment["id"], payload)
    service.result(payment["id"], payload)
    service.result(payment["id"], {**payload, "status": "failed"})
    assert service.get(payment["id"])["status"] == "succeeded"
    assert sum(e["type"] == "payment.succeeded" for e in ctx.store.snapshot()["outbox"]) == 1


def test_failed_attempt_can_be_followed_by_success(setup):
    service, _, _, payment = setup
    payload = {"status": "failed", "amount": payment["amount"], "currency": "RUB"}
    service.result(payment["id"], payload)
    service.result(payment["id"], {**payload, "status": "succeeded"})
    assert service.get(payment["id"])["status"] == "succeeded"


def test_wrong_amount_does_not_change_payment(setup):
    service, _, _, payment = setup
    with pytest.raises(DomainError):
        service.result(payment["id"], {"status": "succeeded", "amount": 1, "currency": "RUB"})
    assert service.get(payment["id"])["status"] == "pending"


def test_refund_requires_paid_payment(setup):
    service, ctx, data, payment = setup
    with pytest.raises(DomainError), ctx.store.transaction() as tx:
        service.handle({"type": "booking.refund_requested", "data": {
            **data, "payment_id": payment["id"], "reason": "cancelled"}}, tx)


def test_refund_is_full_and_idempotent(setup):
    service, ctx, data, payment = setup
    service.result(payment["id"], {"status": "succeeded", "amount": data["amount"], "currency": "RUB"})
    with ctx.store.transaction() as tx:
        service.handle({"type": "booking.refund_requested", "data": {
            **data, "payment_id": payment["id"], "reason": "cancelled"}}, tx)
    service.refund_result(payment["id"], {"status": "succeeded"})
    service.refund_result(payment["id"], {"status": "succeeded"})
    assert service.get(payment["id"])["refunded_amount"] == data["amount"]
    assert sum(e["type"] == "payment.refunded" for e in ctx.store.snapshot()["outbox"]) == 1


def test_unrequested_refund_callback_is_rejected(setup):
    service, _, _, payment = setup
    with pytest.raises(DomainError):
        service.refund_result(payment["id"], {"status": "succeeded"})
