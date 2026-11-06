"""Планирование и демонстрационная доставка без внешних email/SMS-провайдеров."""
from copy import deepcopy
from datetime import timedelta
import logging
from uuid import uuid4

from common.errors import DomainError
from common.time import iso, parse_time

logger = logging.getLogger(__name__)


class Service:
    def __init__(self, ctx):
        self.ctx = ctx

    @staticmethod
    def _get(tx, notification_id):
        row = tx.bucket("notifications").get(notification_id)
        if row is None:
            raise DomainError(404, "Уведомление не найдено")
        return row

    def list(self, user_id=None, offset=0, limit=100):
        with self.ctx.store.transaction() as tx:
            rows = list(tx.bucket("notifications").values())
            if user_id:
                rows = [row for row in rows if row["user_id"] == user_id]
            return deepcopy(rows[offset:offset + limit])

    def get(self, notification_id):
        with self.ctx.store.transaction() as tx:
            return deepcopy(self._get(tx, notification_id))

    def _create(self, tx, data, dedupe_key=None):
        if dedupe_key and dedupe_key in tx.bucket("dedupe"):
            return self._get(tx, tx.bucket("dedupe")[dedupe_key])
        notification_id = str(uuid4())
        row = {"id": notification_id, "user_id": str(data["user_id"]),
               "booking_id": str(data["booking_id"]) if data.get("booking_id") else None,
               "kind": data["kind"], "message": data["message"], "status": "prepared", "attempts": 0,
               "scheduled_at": iso(parse_time(data["scheduled_at"])) if data.get("scheduled_at") else iso(self.ctx.now()),
               "sent_at": None, "last_error": None, "provider": "demo-log"}
        tx.bucket("notifications")[notification_id] = row
        if dedupe_key:
            tx.bucket("dedupe")[dedupe_key] = notification_id
        return row

    def create(self, data):
        if not data["message"].strip():
            raise DomainError(422, "Текст уведомления не может быть пустым")
        with self.ctx.store.transaction() as tx:
            return deepcopy(self._create(tx, data))

    def handle(self, event, tx):
        kind, data = event["type"], event["data"]
        if kind == "booking.confirmed":
            booking_id = data["booking_id"]
            if booking_id in tx.bucket("cancelled_bookings"):
                return
            self._create(tx, {"user_id": data["user_id"], "booking_id": booking_id,
                              "kind": "confirmation", "message": f"Бронь {booking_id} подтверждена."},
                         f"confirmation:{booking_id}")
            reminder_at = parse_time(data["start_at"]) - timedelta(hours=1)
            if reminder_at > self.ctx.now():
                self._create(tx, {"user_id": data["user_id"], "booking_id": booking_id,
                                  "kind": "reminder", "message": "До начала игры остался один час.",
                                  "scheduled_at": iso(reminder_at)}, f"reminder:{booking_id}")
        elif kind == "payment.refunded":
            self._create(tx, {"user_id": data["user_id"], "booking_id": data["booking_id"],
                              "kind": "refund", "message": f"Возврат {data['amount']} коп. выполнен."},
                         f"refund:{data['payment_id']}")
        elif kind in ("booking.cancelled", "booking.expired"):
            tx.bucket("cancelled_bookings")[data["booking_id"]] = True
            for row in tx.bucket("notifications").values():
                if row["booking_id"] == data["booking_id"] and row["status"] == "prepared":
                    # Не отправляем также устаревшее подтверждение отменённой брони.
                    if row["kind"] in ("reminder", "confirmation"):
                        row["status"] = "cancelled"

    def _send(self, row):
        if row["status"] in ("sent", "failed", "cancelled"):
            return
        if self.ctx.now() < parse_time(row["scheduled_at"]):
            raise DomainError(409, "Время отправки ещё не наступило")
        row["attempts"] += 1
        try:
            user = self.ctx.http.request("users", "GET", f"/api/users/{row['user_id']}")
            contact = user.get("email") or user.get("contact") or user.get("phone")
            if not contact:
                raise DomainError(422, "У пользователя нет контакта для доставки")
            # Это адаптер-заглушка: факт доставки записывается только в локальный журнал.
            logger.info("DEMO notification=%s kind=%s delivery simulated", row["id"], row["kind"])
            row["status"] = "sent"
            row["sent_at"] = iso(self.ctx.now())
            row["last_error"] = None
        except DomainError as exc:
            row["last_error"] = exc.detail
            if row["attempts"] >= 3 or exc.status < 500:
                row["status"] = "failed"
            else:
                row["scheduled_at"] = iso(self.ctx.now() + timedelta(seconds=5 * row["attempts"]))

    def send(self, notification_id):
        with self.ctx.store.transaction() as tx:
            row = self._get(tx, notification_id)
            self._send(row)
            return deepcopy(row)

    def tick(self):
        with self.ctx.store.transaction() as tx:
            for row in tx.bucket("notifications").values():
                if row["status"] == "prepared" and self.ctx.now() >= parse_time(row["scheduled_at"]):
                    self._send(row)
