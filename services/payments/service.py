"""Учёт платежей. Провайдер учебный: деньги не списываются."""
from copy import deepcopy
from uuid import uuid4

from common.errors import DomainError
from common.time import iso


class Service:
    def __init__(self, ctx):
        self.ctx = ctx

    @staticmethod
    def _get(tx, payment_id):
        payment = tx.bucket("payments").get(payment_id)
        if payment is None:
            raise DomainError(404, "Платёж не найден")
        return payment

    @staticmethod
    def _payload(payment):
        return {"payment_id": payment["id"], **{
            key: payment[key] for key in ("booking_id", "user_id", "amount", "currency")}}

    def list(self, booking_id=None, offset=0, limit=100):
        with self.ctx.store.transaction() as tx:
            rows = list(tx.bucket("payments").values())
            if booking_id:
                rows = [row for row in rows if row["booking_id"] == booking_id]
            return deepcopy(rows[offset:offset + limit])

    def get(self, payment_id):
        with self.ctx.store.transaction() as tx:
            return deepcopy(self._get(tx, payment_id))

    def handle(self, event, tx):
        kind, data = event["type"], event["data"]
        if kind == "booking.payment_requested":
            index = tx.bucket("by_booking")
            if data["booking_id"] in index:
                existing = self._get(tx, index[data["booking_id"]])
                if existing["amount"] != data["amount"] or existing["currency"] != data["currency"]:
                    raise DomainError(409, "Повторная заявка изменила сумму платежа")
                return
            if type(data["amount"]) is not int or data["amount"] <= 0 or data["currency"] != "RUB":
                raise DomainError(422, "Платёж требует положительную сумму в копейках и RUB")
            payment_id = str(uuid4())
            payment = {"id": payment_id, "booking_id": data["booking_id"], "user_id": data["user_id"],
                       "amount": data["amount"], "currency": data["currency"], "status": "pending",
                       "refund_status": None, "refunded_amount": 0, "created_at": iso(self.ctx.now()),
                       "payment_url": f"/api/payments/{payment_id}:result", "provider": "demo"}
            tx.bucket("payments")[payment_id] = payment
            index[data["booking_id"]] = payment_id
            tx.emit("payment.created", {"booking_id": payment["booking_id"], "payment_id": payment_id,
                                        "payment_url": payment["payment_url"]}, payment["booking_id"])
        elif kind == "booking.refund_requested":
            payment = self._get(tx, data["payment_id"])
            if any(payment[key] != data[key] for key in ("booking_id", "user_id", "amount", "currency")):
                raise DomainError(409, "Возврат не соответствует платежу")
            if payment["status"] != "succeeded":
                raise DomainError(409, "Вернуть можно только полученную оплату")
            if payment["refund_status"] in ("pending", "succeeded"):
                return
            payment["refund_status"] = "pending"
            payment["refund_reason"] = data["reason"]

    def result(self, payment_id, data):
        with self.ctx.store.transaction() as tx:
            payment = self._get(tx, payment_id)
            if data["amount"] != payment["amount"] or data["currency"] != payment["currency"]:
                raise DomainError(409, "Сумма или валюта callback не совпадает с платежом")
            # Успешная оплата финальна; повторный или запоздалый отрицательный callback безопасен.
            if payment["status"] == "succeeded" or payment["status"] == data["status"]:
                return deepcopy(payment)
            payment["status"] = data["status"]
            tx.emit("payment.succeeded" if data["status"] == "succeeded" else "payment.failed",
                    self._payload(payment), payment["booking_id"])
            return deepcopy(payment)

    def refund_result(self, payment_id, data):
        with self.ctx.store.transaction() as tx:
            payment = self._get(tx, payment_id)
            if payment["refund_status"] == "succeeded":
                return deepcopy(payment)
            if payment["refund_status"] not in ("pending", "failed"):
                raise DomainError(409, "Возврат не запрошен")
            if payment["refund_status"] == data["status"]:
                return deepcopy(payment)
            payment["refund_status"] = data["status"]
            if data["status"] == "succeeded":
                payment["refunded_amount"] = payment["amount"]
            tx.emit("payment.refunded" if data["status"] == "succeeded" else "payment.refund_failed",
                    self._payload(payment), payment["booking_id"])
            return deepcopy(payment)
