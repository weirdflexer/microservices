from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from common.errors import DomainError
from common.store import MemoryStore
from services.schedule import Service


class FakeHTTP:
    def request(self, *args, **kwargs):
        return {"published": True}


def setup():
    clock = [datetime(2030, 1, 1, 8, tzinfo=timezone.utc)]
    ctx = SimpleNamespace(store=MemoryStore("schedule"), now=lambda: clock[0], http=FakeHTTP())
    service = Service(ctx)
    table_id = str(uuid4())
    service.set_schedule(table_id, {"opens_at": "2030-01-01T09:00:00+00:00", "closes_at": "2030-01-01T23:00:00+00:00"})
    data = {"booking_id": str(uuid4()), "table_id": table_id, "start_at": "2030-01-01T12:00:00+00:00", "end_at": "2030-01-01T13:00:00+00:00"}
    return service, ctx, clock, data


def test_concurrent_overlapping_holds_have_single_winner():
    service, ctx, clock, data = setup()
    def reserve(_):
        try:
            return service.hold({**data, "booking_id": str(uuid4())})
        except DomainError:
            return None
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(16)))
    assert sum(result is not None for result in results) == 1


def test_adjacent_intervals_are_allowed_and_same_booking_is_idempotent():
    service, ctx, clock, data = setup()
    hold = service.hold(data)
    assert service.hold(data)["id"] == hold["id"]
    adjacent = service.hold({**data, "booking_id": str(uuid4()), "start_at": data["end_at"], "end_at": "2030-01-01T14:00:00+00:00"})
    assert adjacent["id"] != hold["id"]


def test_expired_hold_cannot_be_confirmed_and_stops_blocking():
    service, ctx, clock, data = setup()
    hold = service.hold(data)
    clock[0] += timedelta(minutes=10)
    with pytest.raises(DomainError):
        service.confirm(hold["id"], data["booking_id"])
    assert service.availability(data["table_id"], data["start_at"], data["end_at"])["available"]
    service.tick()
    with ctx.store.transaction() as tx:
        assert tx.bucket("holds")[hold["id"]]["status"] == "expired"


def test_confirmed_hold_survives_timer_and_can_only_be_released_by_owner():
    service, ctx, clock, data = setup()
    hold = service.hold(data)
    assert service.confirm(hold["id"], data["booking_id"])["status"] == "confirmed"
    clock[0] += timedelta(minutes=11)
    service.tick()
    assert service.confirm(hold["id"], data["booking_id"])["status"] == "confirmed"
    with pytest.raises(DomainError):
        service.release(hold["id"], str(uuid4()))
    assert not service.availability(data["table_id"], data["start_at"], data["end_at"])["available"]
    service.release(hold["id"], data["booking_id"])
    assert service.release(hold["id"], data["booking_id"])["status"] == "released"


def test_schedule_change_cannot_break_active_hold():
    service, ctx, clock, data = setup()
    service.hold(data)
    with pytest.raises(DomainError):
        service.set_schedule(data["table_id"], {"opens_at": "2030-01-01T14:00:00+00:00", "closes_at": "2030-01-01T23:00:00+00:00"})


def test_cancel_event_releases_only_matching_booking():
    service, ctx, clock, data = setup()
    hold = service.hold(data)
    with ctx.store.transaction() as tx:
        service.handle({"type": "booking.cancelled", "data": {"booking_id": str(uuid4()), "hold_id": hold["id"]}}, tx)
    assert not service.availability(data["table_id"], data["start_at"], data["end_at"])["available"]
    with ctx.store.transaction() as tx:
        service.handle({"type": "booking.cancelled", "data": {"booking_id": data["booking_id"], "hold_id": hold["id"]}}, tx)
    assert service.availability(data["table_id"], data["start_at"], data["end_at"])["available"]
