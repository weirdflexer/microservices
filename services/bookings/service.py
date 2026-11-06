"""Жизненный цикл брони и компенсации распределённого оформления."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from common.errors import DomainError
from common.time import iso, parse_time, validate_interval


class Service:
    def __init__(self, ctx):
        self.ctx = ctx

    @staticmethod
    def _get(tx, booking_id):
        booking = tx.bucket("bookings").get(booking_id)
        if booking is None:
            raise DomainError(404, "Бронь не найдена")
        return booking

    @staticmethod
    def _payload(booking):
        return {"booking_id": booking["id"], **{
            key: booking[key] for key in
            ("user_id", "table_id", "start_at", "end_at", "amount", "currency")}}

    def list(self, user_id=None, status=None, offset=0, limit=100):
        with self.ctx.store.transaction() as tx:
            rows = list(tx.bucket("bookings").values())
            if user_id:
                rows = [row for row in rows if row["user_id"] == user_id]
            if status:
                rows = [row for row in rows if row["status"] == status]
            return deepcopy(rows[offset:offset + limit])

    def get(self, booking_id):
        with self.ctx.store.transaction() as tx:
            return deepcopy(self._get(tx, booking_id))

    def create(self, data):
        start, end = validate_interval(data["start_at"], data["end_at"], self.ctx.now())
        user_id, table_id, quote_id = (str(data[key]) for key in
                                      ("user_id", "table_id", "quote_id"))
        self.ctx.http.request("users", "GET", f"/api/users/{user_id}")
        interval = {"table_id": table_id, "start_at": iso(start), "end_at": iso(end)}
        quote = self.ctx.http.request("pricing", "POST", f"/api/quotes/{quote_id}:validate", json=interval)
        booking_id = str(uuid4())
        with self.ctx.store.transaction() as tx:
            hold = self.ctx.http.request("schedule", "POST", "/api/holds", json={
                "booking_id": booking_id, **interval})
            booking = {"id": booking_id, "user_id": user_id, "quote_id": quote_id,
                       **interval, "amount": quote["amount"], "currency": quote["currency"],
                       "hold_id": hold["id"], "expires_at": hold["expires_at"],
                       "status": "waiting_payment", "payment_id": None, "payment_url": None,
                       "paid": False, "refund_status": None, "payment_attempts": [],
                       "created_at": iso(self.ctx.now())}
            tx.bucket("bookings")[booking_id] = booking
            tx.emit("booking.payment_requested", {
                **self._payload(booking), "expires_at": booking["expires_at"]}, booking_id)
            return deepcopy(booking)

    def _refund(self, tx, booking, reason):
        if booking["refund_status"] in ("pending", "succeeded"):
            return
        booking["refund_status"] = "pending"
        tx.emit("booking.refund_requested", {
            "booking_id": booking["id"], "user_id": booking["user_id"],
            "payment_id": booking["payment_id"], "amount": booking["amount"],
            "currency": booking["currency"], "reason": reason}, booking["id"])

    def _closed_event(self, tx, booking, event_type):
        tx.emit(event_type, {**self._payload(booking), "hold_id": booking["hold_id"]}, booking["id"])

    def cancel(self, booking_id):
        with self.ctx.store.transaction() as tx:
            booking = self._get(tx, booking_id)
            if booking["status"] == "cancelled":
                return deepcopy(booking)
            if booking["status"] not in ("waiting_payment", "confirmed"):
                raise DomainError(409, "Эту бронь уже нельзя отменить")
            start = parse_time(booking["start_at"])
            if self.ctx.now() >= start:
                raise DomainError(409, "Отмена разрешена только до начала игры")
            booking["status"] = "cancelled"
            booking["cancelled_at"] = iso(self.ctx.now())
            self._closed_event(tx, booking, "booking.cancelled")
            if booking["paid"] and start - self.ctx.now() >= timedelta(hours=2):
                self._refund(tx, booking, "cancelled_at_least_two_hours_before_start")
            return deepcopy(booking)

    def handle(self, event, tx):
        kind, data = event["type"], event["data"]
        relevant = {"payment.created", "payment.succeeded", "payment.failed", "payment.refunded",
                    "payment.refund_failed", "schedule.hold_expired", "session.completed", "session.no_show"}
        if kind not in relevant:
            return
        booking = self._get(tx, data["booking_id"])
        if kind.startswith("payment."):
            incoming_id = data["payment_id"]
            if booking["payment_id"] not in (None, incoming_id):
                raise DomainError(409, "Платёж не принадлежит брони")
            if kind != "payment.created" and (
                    data["amount"] != booking["amount"] or data["currency"] != booking["currency"]):
                raise DomainError(409, "Сумма или валюта платежа не совпадает с бронью")
            booking["payment_id"] = incoming_id
        if kind == "payment.created":
            booking["payment_url"] = data["payment_url"]
        elif kind == "payment.failed":
            if not booking["paid"] and "failed" not in booking["payment_attempts"]:
                booking["payment_attempts"].append("failed")
        elif kind == "payment.succeeded":
            was_paid = booking["paid"]
            booking["paid"] = True
            if booking["status"] in ("cancelled", "expired", "rejected"):
                # A duplicate success after a paid, non-refundable cancellation is not a late payment.
                if not was_paid:
                    self._refund(tx, booking, "late_payment")
                return
            if booking["status"] != "waiting_payment":
                return
            try:
                self.ctx.http.request("schedule", "POST", f"/api/holds/{booking['hold_id']}:confirm",
                                      json={"booking_id": booking["id"]})
            except DomainError as exc:
                if exc.status >= 500:
                    # Неизвестный результат вызова: повторяем idempotent confirm, а не возвращаем деньги.
                    raise
                booking["status"] = "expired"
                self._closed_event(tx, booking, "booking.expired")
                self._refund(tx, booking, "hold_cannot_be_confirmed")
                return
            booking["status"] = "confirmed"
            tx.emit("booking.confirmed", self._payload(booking), booking["id"])
        elif kind == "schedule.hold_expired":
            if data["hold_id"] == booking["hold_id"] and booking["status"] == "waiting_payment":
                booking["status"] = "expired"
                self._closed_event(tx, booking, "booking.expired")
                if booking["paid"]:
                    self._refund(tx, booking, "hold_expired")
        elif kind in ("payment.refunded", "payment.refund_failed"):
            if booking["refund_status"] in ("pending", "failed"):
                booking["refund_status"] = "succeeded" if kind == "payment.refunded" else "failed"
        elif kind in ("session.completed", "session.no_show") and booking["status"] == "confirmed":
            booking["status"] = "completed" if kind == "session.completed" else "no_show"
