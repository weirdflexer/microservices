from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from common.errors import DomainError
from common.store import MemoryStore
from common.time import iso
from services.bookings import Service


@pytest.fixture

def setup():
    now = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)
    ids = {key: str(uuid4()) for key in ("user_id", "table_id", "quote_id", "payment_id", "hold_id")}
    calls = []
    def request(service, method, path, json=None):
        calls.append((service, method, path, json))
        if service == "pricing":
            return {"amount": 120000, "currency": "RUB"}
        if service == "schedule":
            return {"id": ids["hold_id"], "expires_at": iso(now + timedelta(minutes=10))}
        return {"id": ids["user_id"]}
    ctx = SimpleNamespace(store=MemoryStore("bookings"), now=lambda: now,
                          http=SimpleNamespace(request=request))
    service = Service(ctx)
    data = {key: ids[key] for key in ("user_id", "table_id", "quote_id")}
    data.update(start_at=iso(now + timedelta(hours=3)), end_at=iso(now + timedelta(hours=5)))
    return service, ctx, ids, data, calls


def deliver(service, ctx, kind, data):
    with ctx.store.transaction() as tx:
        service.handle({"type": kind, "data": data}, tx)


def paid_payload(booking, ids):
    return {"booking_id": booking["id"], "payment_id": ids["payment_id"],
            "amount": booking["amount"], "currency": booking["currency"]}


def test_creation_checks_dependencies_and_emits_request(setup):
    service, ctx, _, data, calls = setup
    booking = service.create(data)
    assert booking["status"] == "waiting_payment"
    assert [c[0] for c in calls] == ["users", "pricing", "schedule"]
    event = ctx.store.snapshot()["outbox"][0]
    assert event["type"] == "booking.payment_requested"
    assert event["data"]["amount"] == 120000


def test_confirmation_waits_for_hold_and_is_idempotent(setup):
    service, ctx, ids, data, calls = setup
    booking = service.create(data)
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    assert service.get(booking["id"])["status"] == "confirmed"
    assert sum(e["type"] == "booking.confirmed" for e in ctx.store.snapshot()["outbox"]) == 1
    assert calls[-1][2].endswith(":confirm")


def test_wrong_payment_amount_cannot_confirm(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    payload = paid_payload(booking, ids)
    payload["amount"] += 1
    with pytest.raises(DomainError, match="Сумма"):
        deliver(service, ctx, "payment.succeeded", payload)
    assert not service.get(booking["id"])["paid"]


def test_temporary_hold_error_rolls_back_and_can_retry(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    original = ctx.http.request
    def fail(*args, **kwargs):
        raise DomainError(503, "network")
    ctx.http.request = fail
    with pytest.raises(DomainError):
        deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    assert service.get(booking["id"])["status"] == "waiting_payment"
    assert not service.get(booking["id"])["paid"]
    ctx.http.request = original
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    assert service.get(booking["id"])["status"] == "confirmed"


def test_permanent_hold_failure_requests_compensation(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    def fail(*args, **kwargs):
        raise DomainError(409, "expired")
    ctx.http.request = fail
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    result = service.get(booking["id"])
    assert (result["status"], result["refund_status"]) == ("expired", "pending")


def test_cancel_paid_booking_more_than_two_hours_refunds_once(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    service.cancel(booking["id"])
    service.cancel(booking["id"])
    assert sum(e["type"] == "booking.refund_requested" for e in ctx.store.snapshot()["outbox"]) == 1


def test_duplicate_payment_after_nonrefundable_cancel_does_not_refund(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    ctx.now = lambda: datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    service.cancel(booking["id"])
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    assert service.get(booking["id"])["refund_status"] is None


def test_late_payment_after_unpaid_cancel_always_refunds(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    ctx.now = lambda: datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    service.cancel(booking["id"])
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    assert service.get(booking["id"])["refund_status"] == "pending"


def test_stale_expiry_does_not_cancel_confirmed_booking(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    deliver(service, ctx, "schedule.hold_expired", {"booking_id": booking["id"], "hold_id": ids["hold_id"]})
    assert service.get(booking["id"])["status"] == "confirmed"


def test_cancel_after_start_is_rejected(setup):
    service, ctx, _, data, _ = setup
    booking = service.create(data)
    ctx.now = lambda: datetime(2030, 1, 1, 13, tzinfo=timezone.utc)
    with pytest.raises(DomainError):
        service.cancel(booking["id"])


def test_refund_result_and_completed_session_update_status(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    deliver(service, ctx, "session.completed", {"booking_id": booking["id"], "session_id": str(uuid4())})
    assert service.get(booking["id"])["status"] == "completed"


@pytest.mark.parametrize("minutes_before,expected", [(120, "pending"), (119, None)])
def test_refund_window_boundary(setup, minutes_before, expected):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    start = datetime(2030, 1, 1, 13, tzinfo=timezone.utc)
    ctx.now = lambda: start - timedelta(minutes=minutes_before)
    assert service.cancel(booking["id"])["refund_status"] == expected


def test_payment_created_failed_and_refund_results_are_recorded(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    payload = paid_payload(booking, ids)
    deliver(service, ctx, "payment.created", {"booking_id": booking["id"],
            "payment_id": ids["payment_id"], "payment_url": "/demo/payment"})
    deliver(service, ctx, "payment.failed", payload)
    assert service.get(booking["id"])["payment_url"] == "/demo/payment"
    assert service.get(booking["id"])["payment_attempts"] == ["failed"]
    deliver(service, ctx, "payment.succeeded", payload)
    service.cancel(booking["id"])
    deliver(service, ctx, "payment.refund_failed", payload)
    assert service.get(booking["id"])["refund_status"] == "failed"
    deliver(service, ctx, "payment.refunded", payload)
    deliver(service, ctx, "payment.refund_failed", payload)
    assert service.get(booking["id"])["refund_status"] == "succeeded"


def test_expiry_only_affects_own_waiting_hold_and_late_payment_compensates(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    deliver(service, ctx, "schedule.hold_expired", {"booking_id": booking["id"], "hold_id": str(uuid4())})
    assert service.get(booking["id"])["status"] == "waiting_payment"
    deliver(service, ctx, "schedule.hold_expired", {"booking_id": booking["id"], "hold_id": ids["hold_id"]})
    assert service.get(booking["id"])["status"] == "expired"
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    assert service.get(booking["id"])["refund_status"] == "pending"


def test_no_show_closes_confirmed_booking_without_refund(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    deliver(service, ctx, "session.no_show", {"booking_id": booking["id"], "session_id": str(uuid4())})
    result = service.get(booking["id"])
    assert result["status"] == "no_show"
    assert result["refund_status"] is None


def test_second_payment_id_rejected(setup):
    service, ctx, ids, data, _ = setup
    booking = service.create(data)
    deliver(service, ctx, "payment.succeeded", paid_payload(booking, ids))
    with pytest.raises(DomainError):
        deliver(service, ctx, "payment.succeeded", {**paid_payload(booking, ids), "payment_id": str(uuid4())})
