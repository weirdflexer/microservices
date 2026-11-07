"""Concurrency tests exercise actual PostgreSQL transactions through public HTTP."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def test_overlapping_holds_have_one_winner_and_adjacent_hold_is_allowed(live_stack):
    stack = live_stack
    prepared = stack.prepare()
    base = {"table_id": prepared["table"]["id"], **prepared["interval"]}
    barrier = Barrier(8)

    def reserve(_):
        body = {**base, "booking_id": str(uuid4())}
        barrier.wait(timeout=10)
        return body, stack.raw("schedule", "POST", "/api/holds", body=body)

    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(reserve, range(8)))
    statuses = [response.status_code for _, response in attempts]
    assert statuses.count(201) == 1 and statuses.count(409) == 7, [
        (response.status_code, response.text) for _, response in attempts]
    owner_body, winner_response = next(row for row in attempts if row[1].status_code == 201)
    winner = winner_response.json()
    repeated = stack.request("schedule", "POST", "/api/holds", status=201, body=owner_body)
    assert repeated["id"] == winner["id"]
    equivalent = {**owner_body, "start_at": owner_body["start_at"].replace("+00:00", "Z"),
                  "end_at": owner_body["end_at"].replace("+00:00", "Z")}
    assert stack.request("schedule", "POST", "/api/holds", status=201, body=equivalent)["id"] == winner["id"]
    assert len(stack.availability(prepared)["busy_intervals"]) == 1

    adjacent_body = {"table_id": base["table_id"], "booking_id": str(uuid4()),
                     "start_at": base["end_at"],
                     "end_at": (datetime.fromisoformat(base["end_at"]) + timedelta(hours=1)).isoformat()}
    adjacent = stack.request("schedule", "POST", "/api/holds", status=201, body=adjacent_body)
    narrowed_window = {"opens_at": base["end_at"], "closes_at": prepared["schedule"]["closes_at"]}
    stack.request("schedule", "PUT", f"/api/tables/{base['table_id']}/schedule", status=409, body=narrowed_window)
    confirmed = stack.request("schedule", "POST", f"/api/holds/{winner['id']}:confirm",
                              body={"booking_id": owner_body["booking_id"]})
    assert confirmed["status"] == "confirmed"
    assert stack.request("schedule", "POST", f"/api/holds/{winner['id']}:confirm",
                         body={"booking_id": owner_body["booking_id"]})["status"] == "confirmed"
    for hold, body in ((winner, owner_body), (adjacent, adjacent_body)):
        released = stack.request("schedule", "DELETE", f"/api/holds/{hold['id']}", params={"booking_id": body["booking_id"]})
        assert released["status"] == "released"
    assert stack.availability(prepared)["available"] is True


def test_simultaneous_booking_requests_create_one_booking_and_one_payment(live_stack):
    stack = live_stack
    prepared = stack.prepare()
    body = stack.booking_body(prepared)
    barrier = Barrier(6)

    def create(_):
        barrier.wait(timeout=10)
        return stack.raw("bookings", "POST", "/api/bookings", body=body)

    with ThreadPoolExecutor(max_workers=6) as pool:
        attempts = list(pool.map(create, range(6)))
    statuses = [response.status_code for response in attempts]
    assert statuses.count(201) == 1 and statuses.count(409) == 5, [
        (response.status_code, response.text) for response in attempts]
    winner = next(response.json() for response in attempts if response.status_code == 201)
    booking = stack.wait(lambda: stack.booking(winner["id"]), lambda row: row.get("payment_id") is not None)
    bookings = stack.request("bookings", "GET", "/api/bookings", params={"user_id": prepared["user"]["id"]})
    assert len(bookings) == 1
    payments = stack.request("payments", "GET", "/api/payments", params={"booking_id": booking["id"]})
    assert len(payments) == 1
    assert len(stack.availability(prepared)["busy_intervals"]) == 1
    stack.request("bookings", "POST", f"/api/bookings/{booking['id']}:cancel")
    stack.wait(lambda: stack.availability(prepared), lambda row: row["available"] is True)
