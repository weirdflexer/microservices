from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from common.errors import DomainError
from common.store import MemoryStore
from common.time import iso
from services.notifications import Service


@pytest.fixture

def setup():
    now = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
    calls = []
    def request(service, method, path, json=None):
        calls.append((service, method, path))
        return {"email": "student@example.test"}
    ctx = SimpleNamespace(store=MemoryStore("notifications"), now=lambda: now,
                          http=SimpleNamespace(request=request))
    return Service(ctx), ctx, calls


def test_send_fetches_contact_and_does_not_send_twice(setup):
    service, ctx, calls = setup
    row = service.create({"user_id": str(uuid4()), "kind": "custom", "message": "Привет"})
    assert service.send(row["id"])["status"] == "sent"
    service.send(row["id"])
    assert len(calls) == 1
    assert service.get(row["id"])["attempts"] == 1


def test_confirmation_schedules_reminder_and_cancellation_suppresses_it(setup):
    service, ctx, _ = setup
    data = {"user_id": str(uuid4()), "booking_id": str(uuid4()),
            "start_at": iso(ctx.now() + timedelta(hours=3))}
    with ctx.store.transaction() as tx:
        service.handle({"type": "booking.confirmed", "data": data}, tx)
        service.handle({"type": "booking.confirmed", "data": data}, tx)
    assert sorted(row["kind"] for row in service.list()) == ["confirmation", "reminder"]
    with ctx.store.transaction() as tx:
        service.handle({"type": "booking.cancelled", "data": data}, tx)
    service.tick()
    assert all(row["status"] == "cancelled" for row in service.list())


def test_no_reminder_when_booking_starts_within_hour(setup):
    service, ctx, _ = setup
    with ctx.store.transaction() as tx:
        service.handle({"type": "booking.confirmed", "data": {
            "user_id": str(uuid4()), "booking_id": str(uuid4()),
            "start_at": iso(ctx.now() + timedelta(minutes=30))}}, tx)
    assert len(service.list()) == 1


def test_transient_delivery_retries_at_most_three_times(setup):
    service, ctx, calls = setup
    def fail(*args, **kwargs):
        raise DomainError(503, "users temporarily unavailable")
    ctx.http.request = fail
    row = service.create({"user_id": str(uuid4()), "kind": "custom", "message": "Привет"})
    for minute in range(4):
        ctx.now = lambda minute=minute: datetime(2030, 1, 1, 10, minute, tzinfo=timezone.utc)
        service.tick()
    result = service.get(row["id"])
    assert result["status"] == "failed"
    assert result["attempts"] == 3


def test_scheduled_notification_cannot_send_early(setup):
    service, ctx, calls = setup
    row = service.create({"user_id": str(uuid4()), "kind": "reminder", "message": "Скоро игра",
                          "scheduled_at": iso(ctx.now() + timedelta(hours=1))})
    with pytest.raises(DomainError):
        service.send(row["id"])
    assert not calls
    assert service.get(row["id"])["attempts"] == 0


def test_refund_notification_is_deduplicated(setup):
    service, ctx, _ = setup
    data = {"user_id": str(uuid4()), "booking_id": str(uuid4()), "payment_id": str(uuid4()), "amount": 1000}
    with ctx.store.transaction() as tx:
        service.handle({"type": "payment.refunded", "data": data}, tx)
        service.handle({"type": "payment.refunded", "data": data}, tx)
    assert len(service.list()) == 1


def test_scheduled_at_normalizes_offset_to_utc_without_changing_instant(setup):
    service, ctx, _ = setup
    row = service.create({"user_id": str(uuid4()), "kind": "reminder", "message": "Скоро игра",
                          "scheduled_at": "2030-01-01T15:30:00+03:00"})
    assert row["scheduled_at"] == "2030-01-01T12:30:00+00:00"
    assert service.get(row["id"])["scheduled_at"] == "2030-01-01T12:30:00+00:00"
