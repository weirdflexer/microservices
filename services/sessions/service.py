"""Посещение создаётся событием подтверждения брони; старт и неявка исключают друг друга."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from common.errors import DomainError
from common.time import iso, parse_time


class Service:
    def __init__(self, ctx):
        self.ctx = ctx

    @staticmethod
    def _get(tx, session_id):
        session = tx.bucket("sessions").get(session_id)
        if session is None:
            raise DomainError(404, "Посещение не найдено")
        return session

    def list(self, booking_id=None, offset=0, limit=100):
        with self.ctx.store.transaction() as tx:
            rows = list(tx.bucket("sessions").values())
            if booking_id:
                rows = [row for row in rows if row["booking_id"] == booking_id]
            return deepcopy(rows[offset:offset + limit])

    def get(self, session_id):
        with self.ctx.store.transaction() as tx:
            return deepcopy(self._get(tx, session_id))

    def handle(self, event, tx):
        kind, data = event["type"], event["data"]
        if kind == "booking.confirmed":
            booking_id = data["booking_id"]
            if booking_id in tx.bucket("by_booking") or booking_id in tx.bucket("cancelled_bookings"):
                return
            session_id = str(uuid4())
            session = {"id": session_id, **{key: data[key] for key in
                       ("booking_id", "user_id", "table_id", "start_at", "end_at")},
                       "status": "expected", "started_at": None, "completed_at": None}
            tx.bucket("sessions")[session_id] = session
            tx.bucket("by_booking")[booking_id] = session_id
        elif kind in ("booking.cancelled", "booking.expired"):
            booking_id = data["booking_id"]
            tx.bucket("cancelled_bookings")[booking_id] = True
            session_id = tx.bucket("by_booking").get(booking_id)
            if session_id:
                session = self._get(tx, session_id)
                if session["status"] == "expected":
                    session["status"] = "cancelled"

    def start(self, session_id):
        with self.ctx.store.transaction() as tx:
            session = self._get(tx, session_id)
            if session["status"] == "active":
                return deepcopy(session)
            if session["status"] != "expected":
                raise DomainError(409, "Посещение уже не ожидается")
            start = parse_time(session["start_at"])
            if not start <= self.ctx.now() < start + timedelta(minutes=15):
                raise DomainError(409, "Начало возможно только в первые 15 минут брони")
            booking = self.ctx.http.request("bookings", "GET", f"/api/bookings/{session['booking_id']}")
            if (booking["status"] != "confirmed"
                    or any(booking[key] != session[key] for key in ("user_id", "table_id"))
                    or any(parse_time(booking[key]) != parse_time(session[key]) for key in ("start_at", "end_at"))):
                raise DomainError(409, "Бронь не подтверждена или не соответствует посещению")
            session["status"] = "active"
            session["started_at"] = iso(self.ctx.now())
            return deepcopy(session)

    def _complete(self, tx, session):
        session["status"] = "completed"
        session["completed_at"] = iso(self.ctx.now())
        tx.emit("session.completed", {"booking_id": session["booking_id"], "session_id": session["id"]},
                session["booking_id"])

    def complete(self, session_id):
        with self.ctx.store.transaction() as tx:
            session = self._get(tx, session_id)
            if session["status"] == "completed":
                return deepcopy(session)
            if session["status"] != "active":
                raise DomainError(409, "Завершить можно только начатый сеанс")
            if self.ctx.now() < parse_time(session["started_at"]):
                raise DomainError(409, "Окончание не может предшествовать началу")
            self._complete(tx, session)
            return deepcopy(session)

    def tick(self):
        with self.ctx.store.transaction() as tx:
            for session in tx.bucket("sessions").values():
                if session["status"] == "expected" and self.ctx.now() >= parse_time(session["start_at"]) + timedelta(minutes=15):
                    session["status"] = "no_show"
                    tx.emit("session.no_show", {"booking_id": session["booking_id"], "session_id": session["id"]},
                            session["booking_id"])
                elif session["status"] == "active" and self.ctx.now() >= parse_time(session["end_at"]):
                    self._complete(tx, session)
