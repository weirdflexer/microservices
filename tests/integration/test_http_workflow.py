"""Real HTTP + broker flows across Python, Node.js and Go processes."""

import pytest

pytestmark = pytest.mark.integration


def test_booking_payment_cancellation_refund_reaches_all_eight_services(live_stack):
    stack = live_stack
    prepared = stack.prepare()
    booking = stack.pay(stack.book(prepared))
    assert booking["paid"] is True
    assert booking["currency"] == "RUB"
    assert stack.availability(prepared)["available"] is False
    visits = stack.wait(lambda: stack.visits(booking["id"]), lambda rows: len(rows) == 1,
                        description="Node expected visit")
    visit = visits[0]
    assert visit["status"] == "expected"
    # Start/completion cannot occur several hours before the booked interval.
    stack.request("sessions", "POST", f"/api/sessions/{visit['id']}:start", status=409)
    stack.request("sessions", "POST", f"/api/sessions/{visit['id']}:complete", status=409)
    notices = stack.wait(lambda: stack.notices(prepared["user"]["id"]),
                         lambda rows: {row["kind"] for row in rows} == {"confirmation", "reminder"},
                         description="Python confirmation and reminder")
    confirmation = next(row for row in notices if row["kind"] == "confirmation")
    sent = stack.request("notifications", "POST", f"/api/notifications/{confirmation['id']}:send")
    assert sent["status"] == "sent"
    sent_again = stack.request("notifications", "POST", f"/api/notifications/{confirmation['id']}:send")
    assert sent_again["attempts"] == sent["attempts"]

    cancelled = stack.request("bookings", "POST", f"/api/bookings/{booking['id']}:cancel")
    assert cancelled["status"] == "cancelled"
    stack.request("bookings", "POST", f"/api/bookings/{booking['id']}:cancel")
    stack.wait(lambda: stack.payment(booking["payment_id"]), lambda row: row["refund_status"] == "pending",
               description="asynchronous refund request")
    stack.wait(lambda: stack.visits(booking["id"]), lambda rows: len(rows) == 1 and rows[0]["status"] == "cancelled")
    stack.wait(lambda: stack.availability(prepared), lambda row: row["available"] is True)
    stack.wait(lambda: stack.notices(prepared["user"]["id"]),
               lambda rows: any(row["kind"] == "reminder" and row["status"] == "cancelled" for row in rows))

    path = f"/api/payments/{booking['payment_id']}:refund-result"
    refunded = stack.request("payments", "POST", path, provider=True, body={"status": "succeeded"})
    assert refunded["refunded_amount"] == booking["amount"]
    stack.request("payments", "POST", path, provider=True, body={"status": "succeeded"})
    # A delayed failure cannot reverse a completed refund.
    stale = stack.request("payments", "POST", path, provider=True, body={"status": "failed"})
    assert stale["refund_status"] == "succeeded"
    final = stack.wait(lambda: stack.booking(booking["id"]), lambda row: row["refund_status"] == "succeeded")
    assert final["status"] == "cancelled"
    stack.wait(lambda: stack.notices(prepared["user"]["id"]),
               lambda rows: sum(row["kind"] == "refund" for row in rows) == 1)


def test_failed_payment_can_succeed_without_duplicate_visit_or_payment(live_stack):
    stack = live_stack
    prepared = stack.prepare()
    booking = stack.book(prepared)
    failed = stack.callback(booking, outcome="failed")
    assert failed["status"] == "failed"
    waiting = stack.wait(lambda: stack.booking(booking["id"]), lambda row: row["payment_attempts"] == ["failed"])
    assert waiting["status"] == "waiting_payment" and waiting["paid"] is False
    stack.pay(booking)
    stack.callback(booking)
    stale = stack.callback(booking, outcome="failed")
    assert stale["status"] == "succeeded"
    stack.wait(lambda: stack.visits(booking["id"]), lambda rows: len(rows) == 1)
    payments = stack.request("payments", "GET", "/api/payments", params={"booking_id": booking["id"]})
    assert len(payments) == 1
    assert stack.booking(booking["id"])["status"] == "confirmed"
    stack.request("bookings", "POST", f"/api/bookings/{booking['id']}:cancel")


def test_late_payment_after_unpaid_cancellation_is_compensated(live_stack):
    stack = live_stack
    prepared = stack.prepare(hours_ahead=1)
    booking = stack.book(prepared)
    stack.request("bookings", "POST", f"/api/bookings/{booking['id']}:cancel")
    stack.wait(lambda: stack.availability(prepared), lambda row: row["available"] is True)
    stack.callback(booking)
    compensated = stack.wait(lambda: stack.booking(booking["id"]), lambda row: row["refund_status"] == "pending")
    assert compensated["paid"] is True and compensated["status"] == "cancelled"
    stack.wait(lambda: stack.payment(booking["payment_id"]), lambda row: row["refund_status"] == "pending")
    stack.request("payments", "POST", f"/api/payments/{booking['payment_id']}:refund-result",
                  provider=True, body={"status": "succeeded"})
    stack.wait(lambda: stack.booking(booking["id"]), lambda row: row["refund_status"] == "succeeded")
    assert stack.visits(booking["id"]) == []
    assert stack.availability(prepared)["available"] is True


def test_paid_cancellation_inside_two_hours_does_not_refund_on_duplicate_callback(live_stack):
    stack = live_stack
    prepared = stack.prepare(hours_ahead=1)
    booking = stack.pay(stack.book(prepared))
    cancelled = stack.request("bookings", "POST", f"/api/bookings/{booking['id']}:cancel")
    assert cancelled["refund_status"] is None
    stack.callback(booking)
    stack.wait(lambda: stack.availability(prepared), lambda row: row["available"] is True)
    assert stack.booking(booking["id"])["refund_status"] is None
    assert stack.payment(booking["payment_id"])["refund_status"] is None
