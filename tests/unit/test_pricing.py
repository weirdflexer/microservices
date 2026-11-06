from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import pytest
from common.errors import DomainError
from common.store import MemoryStore
from services.pricing import Service


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.table = {"published": True, "game_type": "pool"}

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.table


def setup():
    clock = [datetime(2030, 1, 1, 8, tzinfo=timezone.utc)]
    ctx = SimpleNamespace(store=MemoryStore("pricing"), now=lambda: clock[0], http=FakeHTTP())
    data = {"table_id": str(uuid4()), "start_at": "2030-01-01T12:00:00+00:00", "end_at": "2030-01-01T13:30:00+00:00"}
    return Service(ctx), ctx, clock, data


def test_quote_uses_catalog_and_locked_tariff_version_integer_money():
    service, ctx, clock, data = setup()
    first_tariff = service.publish({"game_type": "pool", "hourly_rate": 101})
    quote = service.quote(data)
    assert quote["amount"] == 152
    assert quote["tariff_id"] == first_tariff["id"]
    assert ctx.http.calls[0][0] == ("catalog", "GET", f'/api/tables/{data["table_id"]}')
    service.publish({"game_type": "pool", "hourly_rate": 200})
    assert service.validate_quote(quote["id"], data)["amount"] == 152
    assert service.quote(data)["amount"] == 300


def test_quote_expires_exactly_at_ten_minutes():
    service, ctx, clock, data = setup()
    service.publish({"game_type": "pool", "hourly_rate": 10000})
    quote = service.quote(data)
    clock[0] += timedelta(minutes=10)
    with pytest.raises(DomainError):
        service.validate_quote(quote["id"], data)


def test_quote_cannot_be_used_for_different_table_or_interval():
    service, ctx, clock, data = setup()
    service.publish({"game_type": "pool", "hourly_rate": 10000})
    quote = service.quote(data)
    for changed in ({**data, "table_id": str(uuid4())}, {**data, "end_at": "2030-01-01T14:00:00+00:00"}):
        with pytest.raises(DomainError):
            service.validate_quote(quote["id"], changed)


def test_unpublished_table_or_missing_tariff_rejected():
    service, ctx, clock, data = setup()
    with pytest.raises(DomainError):
        service.quote(data)
    service.publish({"game_type": "pool", "hourly_rate": 10000})
    ctx.http.table["published"] = False
    with pytest.raises(DomainError):
        service.quote(data)


@pytest.mark.parametrize("start,end", [("12:15", "13:15"), ("12:00", "12:30"), ("12:00", "16:30")])
def test_invalid_booking_interval_rejected(start, end):
    service, ctx, clock, data = setup()
    service.publish({"game_type": "pool", "hourly_rate": 10000})
    with pytest.raises(DomainError):
        service.quote({**data, "start_at": f"2030-01-01T{start}:00+00:00", "end_at": f"2030-01-01T{end}:00+00:00"})
