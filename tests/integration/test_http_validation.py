"""Public API failure contracts must remain compatible after language migration."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("service,path", [
    ("schedule", "/api/holds/not-a-uuid:confirm"),
    ("bookings", "/api/bookings/not-a-uuid"),
    ("payments", "/api/payments/not-a-uuid"),
    ("sessions", "/api/sessions/not-a-uuid"),
])
def test_malformed_path_identifiers_are_422(live_stack, service, path):
    if service == "schedule":
        live_stack.request(service, "POST", path, body={"booking_id": str(uuid4())}, status=422)
    else:
        live_stack.request(service, "GET", path, status=422)


@pytest.mark.parametrize("service,path", [
    ("bookings", "/api/bookings"), ("payments", "/api/payments"), ("sessions", "/api/sessions")])
@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "one"}])
def test_invalid_pagination_is_422(live_stack, service, path, query):
    live_stack.request(service, "GET", path, params=query, status=422)


@pytest.mark.parametrize("service,path,filter_name", [
    ("bookings", "/api/bookings", "user_id"),
    ("payments", "/api/payments", "booking_id"),
    ("sessions", "/api/sessions", "booking_id"),
])
def test_empty_lists_are_arrays_and_identifier_filters_validate(live_stack, service, path, filter_name):
    assert live_stack.request(service, "GET", path, params={filter_name: str(uuid4())}) == []
    live_stack.request(service, "GET", path, params={filter_name: "broken"}, status=422)


@pytest.mark.parametrize("service,path", [
    ("bookings", "/api/bookings"), ("payments", "/api/payments"), ("sessions", "/api/sessions")])
def test_valid_unknown_identifier_is_404(live_stack, service, path):
    error = live_stack.request(service, "GET", f"{path}/{uuid4()}", status=404)
    assert "detail" in error


@pytest.mark.parametrize("case", ["missing", "unknown", "bad_uuid", "naive_time", "off_grid", "too_short", "too_long"])
def test_schedule_body_validation(live_stack, prepared, case):
    body = {"booking_id": str(uuid4()), "table_id": prepared["table"]["id"], **prepared["interval"]}
    start = datetime.fromisoformat(body["start_at"])
    if case == "missing":
        del body["end_at"]
    elif case == "unknown":
        body["price"] = 1
    elif case == "bad_uuid":
        body["booking_id"] = "broken"
    elif case == "naive_time":
        body["start_at"] = start.replace(tzinfo=None).isoformat()
    elif case == "off_grid":
        body["start_at"] = (start + timedelta(minutes=15)).isoformat()
    elif case == "too_short":
        body["end_at"] = (start + timedelta(minutes=30)).isoformat()
    elif case == "too_long":
        body["end_at"] = (start + timedelta(hours=5)).isoformat()
    live_stack.request("schedule", "POST", "/api/holds", body=body, status=422)
    assert live_stack.availability(prepared)["available"] is True


@pytest.mark.parametrize("case", ["missing", "unknown", "bad_uuid", "naive_time", "off_grid"])
def test_booking_body_validation(live_stack, prepared, case):
    body = live_stack.booking_body(prepared)
    if case == "missing":
        del body["quote_id"]
    elif case == "unknown":
        body["amount"] = 1
    elif case == "bad_uuid":
        body["user_id"] = "broken"
    elif case == "naive_time":
        body["start_at"] = datetime.fromisoformat(body["start_at"]).replace(tzinfo=None).isoformat()
    elif case == "off_grid":
        body["start_at"] = (datetime.fromisoformat(body["start_at"]) + timedelta(minutes=15)).isoformat()
    live_stack.request("bookings", "POST", "/api/bookings", body=body, status=422)


def test_schedule_window_and_foreign_hold_validation(live_stack):
    stack = live_stack
    prepared = stack.prepare()
    table_id = prepared["table"]["id"]
    stack.request("schedule", "PUT", f"/api/tables/{table_id}/schedule", status=422,
                  body={"opens_at": prepared["interval"]["end_at"], "closes_at": prepared["interval"]["start_at"]})
    stack.request("schedule", "PUT", f"/api/tables/{table_id}/schedule", status=422,
                  body={**prepared["schedule"], "extra": True})
    body = {"booking_id": str(uuid4()), "table_id": table_id, **prepared["interval"]}
    hold = stack.request("schedule", "POST", "/api/holds", status=201, body=body)
    stack.request("schedule", "POST", f"/api/holds/{hold['id']}:confirm",
                  status=409, body={"booking_id": str(uuid4())})
    stack.request("schedule", "DELETE", f"/api/holds/{hold['id']}",
                  status=409, params={"booking_id": str(uuid4())})
    stack.request("schedule", "DELETE", f"/api/holds/{hold['id']}",
                  status=422, params={"booking_id": "broken"})
    stack.request("schedule", "DELETE", f"/api/holds/{hold['id']}", status=422)
    stack.request("schedule", "DELETE", f"/api/holds/{hold['id']}", params={"booking_id": body["booking_id"]})


def test_callback_authentication_strict_money_and_no_partial_mutation(live_stack):
    stack = live_stack
    prepared = stack.prepare()
    booking = stack.book(prepared)
    path = f"/api/payments/{booking['payment_id']}:result"
    good = {"status": "succeeded", "amount": booking["amount"], "currency": "RUB"}
    for headers in ({}, {"X-Provider-Token": "wrong"}):
        response = stack.raw("payments", "POST", path, body=good, headers=headers)
        assert response.status_code == 401, response.text
    for override in ({"amount": str(booking["amount"])}, {"amount": True}, {"amount": 1.5},
                     {"amount": 0}, {"status": "paid"}, {"currency": "USD"}, {"extra": 1}):
        stack.request("payments", "POST", path, provider=True, status=422, body={**good, **override})
    stack.request("payments", "POST", path, provider=True, status=422, body={"status": "succeeded"})
    stack.request("payments", "POST", path, provider=True, status=409, body={**good, "amount": booking["amount"] + 1})
    stack.request("payments", "POST", f"/api/payments/{booking['payment_id']}:refund-result",
                  provider=True, status=409, body={"status": "succeeded"})
    assert stack.payment(booking["payment_id"])["status"] == "pending"
    assert stack.booking(booking["id"])["paid"] is False
    # Currency is optional in the existing API and defaults to RUB.
    stack.request("payments", "POST", path, provider=True,
                  body={"status": "succeeded", "amount": booking["amount"]})
    stack.wait(lambda: stack.booking(booking["id"]), lambda row: row["status"] == "confirmed")
    stack.request("bookings", "POST", f"/api/bookings/{booking['id']}:cancel")
