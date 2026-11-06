from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from common.errors import DomainError
from common.store import MemoryStore
from common.time import iso
from services.sessions import Service


@pytest.fixture

def setup():
    now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    data = {"booking_id": str(uuid4()), "user_id": str(uuid4()), "table_id": str(uuid4()),
            "start_at": iso(now), "end_at": iso(now + timedelta(hours=2))}
    calls = []
    def request(service, method, path, json=None):
        calls.append((service, method, path))
        return {**data, "id": data["booking_id"], "status": "confirmed"}
    ctx = SimpleNamespace(store=MemoryStore("sessions"), now=lambda: now,
                          http=SimpleNamespace(request=request))
    service = Service(ctx)
    with ctx.store.transaction() as tx:
        service.handle({"type": "booking.confirmed", "data": data}, tx)
    return service, ctx, data, service.list()[0], calls


def test_expected_visit_unique_per_booking(setup):
    service, ctx, data, _, _ = setup
    with ctx.store.transaction() as tx:
        service.handle({"type": "booking.confirmed", "data": data}, tx)
    assert len(service.list()) == 1


def test_start_checks_booking_and_complete_emits_once(setup):
    service, ctx, _, session, calls = setup
    assert service.start(session["id"])["status"] == "active"
    assert calls[0][0] == "bookings"
    service.complete(session["id"])
    service.complete(session["id"])
    assert len(ctx.store.snapshot()["outbox"]) == 1
    assert ctx.store.snapshot()["outbox"][0]["type"] == "session.completed"


def test_start_before_slot_is_rejected(setup):
    service, ctx, _, session, _ = setup
    ctx.now = lambda: datetime(2030, 1, 1, 11, 59, tzinfo=timezone.utc)
    with pytest.raises(DomainError):
        service.start(session["id"])


def test_no_show_boundary_excludes_start(setup):
    service, ctx, _, session, _ = setup
    ctx.now = lambda: datetime(2030, 1, 1, 12, 15, tzinfo=timezone.utc)
    with pytest.raises(DomainError):
        service.start(session["id"])
    service.tick()
    service.tick()
    assert service.get(session["id"])["status"] == "no_show"
    assert len(ctx.store.snapshot()["outbox"]) == 1


def test_active_session_auto_completes_at_reserved_end(setup):
    service, ctx, _, session, _ = setup
    service.start(session["id"])
    ctx.now = lambda: datetime(2030, 1, 1, 14, tzinfo=timezone.utc)
    service.tick()
    assert service.get(session["id"])["status"] == "completed"


def test_cancellation_prevents_no_show_and_out_of_order_creation(setup):
    service, ctx, data, session, _ = setup
    with ctx.store.transaction() as tx:
        service.handle({"type": "booking.cancelled", "data": data}, tx)
    ctx.now = lambda: datetime(2030, 1, 1, 15, tzinfo=timezone.utc)
    service.tick()
    assert service.get(session["id"])["status"] == "cancelled"
    assert ctx.store.snapshot()["outbox"] == []


def test_unconfirmed_booking_cannot_start(setup):
    service, ctx, data, session, _ = setup
    ctx.http.request = lambda *args, **kwargs: {**data, "status": "cancelled"}
    with pytest.raises(DomainError):
        service.start(session["id"])
    assert service.get(session["id"])["status"] == "expected"
