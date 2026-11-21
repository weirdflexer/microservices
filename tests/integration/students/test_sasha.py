from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def request_two_concurrent_holds(live_stack, prepared):
    table_id = prepared["table"]["id"]
    barrier = Barrier(2)

    def reserve(owner):
        barrier.wait(timeout=5)
        return live_stack.raw(
            "schedule",
            "POST",
            "/api/holds",
            body={"booking_id": owner, "table_id": table_id, **prepared["interval"]},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(reserve, [str(uuid4()), str(uuid4())]))


def wait_for_visit(stack, booking_id, status):
    visits = stack.wait(
        lambda: stack.visits(booking_id),
        lambda rows: len(rows) == 1 and rows[0]["status"] == status,
        description=f"visit for booking {booking_id} to become {status}",
    )
    return visits[0]


def test_competing_holds_persist_one_winner(live_stack, prepared, db_bucket):
    """Из двух одновременных запросов в базе остаётся одно удержание."""
    table_id = prepared["table"]["id"]
    responses = request_two_concurrent_holds(live_stack, prepared)

    assert sorted(r.status_code for r in responses) == [201, 409]
    winner = next(r.json() for r in responses if r.status_code == 201)
    holds = db_bucket("schedule", "holds")
    active = [
        hold
        for hold in holds.values()
        if hold["table_id"] == table_id and hold["status"] == "held"
    ]
    assert len(active) == 1
    assert active[0]["id"] == winner["id"]


def test_confirmation_persists_visit(live_stack, confirmed_booking, db_bucket):
    """Подтверждение брони создаёт в базе одно посещение клиента."""
    booking = confirmed_booking
    wait_for_visit(live_stack, booking["id"], "expected")

    visits = [
        visit
        for visit in db_bucket("sessions", "sessions").values()
        if visit["booking_id"] == booking["id"]
    ]
    assert len(visits) == 1
    visit = visits[0]
    assert visit["status"] == "expected"
    assert visit["user_id"] == booking["user_id"]
    assert visit["table_id"] == booking["table_id"]


def test_cancellation_releases_interval(
    live_stack, one_hour_slot, unpaid_booking, cancel_booking
):
    """После отмены другой клиент может занять тот же интервал."""
    case = one_hour_slot
    booking = unpaid_booking
    assert not live_stack.availability(case)["available"]
    cancel_booking(booking)
    live_stack.wait(lambda: live_stack.availability(case), lambda a: a["available"])
    live_stack.request(
        "schedule",
        "POST",
        "/api/holds",
        status=201,
        body={
            "booking_id": str(uuid4()),
            "table_id": case["table"]["id"],
            **case["interval"],
        },
    )


def test_cancelled_visit_cannot_start(live_stack, confirmed_booking, cancel_booking):
    """После отмены подтверждённой брони начать посещение нельзя."""
    booking = confirmed_booking

    visit = wait_for_visit(live_stack, booking["id"], "expected")

    cancel_booking(booking)

    cancelled = wait_for_visit(live_stack, booking["id"], "cancelled")
    assert cancelled["id"] == visit["id"]
    live_stack.request(
        "sessions", "POST", f"/api/sessions/{visit['id']}:start", status=409
    )
