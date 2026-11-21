import pytest

pytestmark = pytest.mark.integration


def confirm_refund(stack, payment_id):
    return stack.request(
        "payments",
        "POST",
        f"/api/payments/{payment_id}:refund-result",
        provider=True,
        body={"status": "succeeded"},
    )


def test_booking_saved_in_postgres(live_stack, one_hour_slot, db_bucket):
    """Созданная бронь хранит в PostgreSQL цену и ID удержания."""
    booking = live_stack.request(
        "bookings",
        "POST",
        "/api/bookings",
        status=201,
        body=live_stack.booking_body(one_hour_slot),
    )
    row = db_bucket("bookings", "bookings")[booking["id"]]
    assert row["status"] == "waiting_payment"
    assert row["amount"] == one_hour_slot["quote"]["amount"]
    assert row["hold_id"] == booking["hold_id"]
    assert row["hold_id"]


def test_invalid_amount_keeps_payment_pending(live_stack, unpaid_booking, db_bucket):
    """Неверная сумма даёт 409 и сохраняет платёж в исходном состоянии."""
    booking = unpaid_booking
    path = f"/api/payments/{booking['payment_id']}:result"
    live_stack.request(
        "payments",
        "POST",
        path,
        status=409,
        provider=True,
        body={
            "status": "succeeded",
            "currency": "RUB",
            "amount": booking["amount"] + 1,
        },
    )
    payment = db_bucket("payments", "payments")[booking["payment_id"]]
    assert payment["status"] == "pending"
    assert payment["amount"] == booking["amount"]


def test_booking_event_creates_linked_payment(live_stack, one_hour_slot):
    """По событию оформления создаётся один платёж с той же суммой."""
    booking = live_stack.book(one_hour_slot)
    payments = live_stack.request(
        "payments", "GET", "/api/payments", params={"booking_id": booking["id"]}
    )
    assert len(payments) == 1
    assert payments[0]["id"] == booking["payment_id"]
    assert payments[0]["booking_id"] == booking["id"]
    assert payments[0]["amount"] == booking["amount"]


def test_refund_reported_to_bookings(live_stack, confirmed_booking, cancel_booking):
    """Два подтверждения возврата возвращают ровно одну стоимость брони."""
    booking = confirmed_booking
    cancel_booking(booking)
    payment_id = booking["payment_id"]
    live_stack.wait(
        lambda: live_stack.payment(payment_id),
        lambda p: p["refund_status"] == "pending",
    )
    first = confirm_refund(live_stack, payment_id)
    repeated = confirm_refund(live_stack, payment_id)

    assert first["refunded_amount"] == booking["amount"]
    assert repeated["refunded_amount"] == booking["amount"]
    live_stack.wait(
        lambda: live_stack.booking(booking["id"]),
        lambda b: b["refund_status"] == "succeeded",
    )
