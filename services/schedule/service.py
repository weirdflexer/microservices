"""Рабочие окна и атомарное владение интервалами [начало, конец)."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from common.errors import DomainError
from common.time import iso, parse_time, validate_interval
from .models import HoldRequest, ScheduleRequest


class Service:
    def __init__(self, ctx):
        self.ctx = ctx

    def _table(self, table_id):
        table = self.ctx.http.request("catalog", "GET", f"/api/tables/{table_id}")
        if not table["published"]:
            raise DomainError(409, "Стол не опубликован")
        return table

    @staticmethod
    def _overlaps(first_start, first_end, second_start, second_end):
        return first_start < second_end and second_start < first_end

    @staticmethod
    def _active(hold, now):
        return hold["status"] == "confirmed" or (hold["status"] == "held" and parse_time(hold["expires_at"]) > now)

    def set_schedule(self, table_id: str, data: ScheduleRequest | dict) -> dict:
        data = ScheduleRequest.model_validate(data).model_dump(mode="json")
        opens, closes = parse_time(data["opens_at"]), parse_time(data["closes_at"])
        if opens >= closes:
            raise DomainError(422, "Конец рабочего окна должен быть позже начала")
        self._table(table_id)
        with self.ctx.store.transaction() as tx:
            now = self.ctx.now()
            for hold in tx.bucket("holds").values():
                if hold["table_id"] == str(table_id) and self._active(hold, now) and parse_time(hold["end_at"]) > now:
                    if parse_time(hold["start_at"]) < opens or parse_time(hold["end_at"]) > closes:
                        raise DomainError(409, "Новый график нарушает действующий резерв или бронь")
            schedule = {"table_id": str(table_id), "opens_at": iso(opens), "closes_at": iso(closes)}
            tx.bucket("schedules")[str(table_id)] = schedule
            return deepcopy(schedule)

    def availability(self, table_id, start_at, end_at) -> dict:
        start, end = validate_interval(start_at, end_at)
        with self.ctx.store.transaction() as tx:
            schedule = tx.bucket("schedules").get(str(table_id))
            if schedule is None:
                raise DomainError(404, "Рабочее окно стола не задано")
            now = self.ctx.now()
            busy = [deepcopy(h) for h in tx.bucket("holds").values()
                    if h["table_id"] == str(table_id) and self._active(h, now)
                    and self._overlaps(start, end, parse_time(h["start_at"]), parse_time(h["end_at"]))]
            in_window = parse_time(schedule["opens_at"]) <= start and end <= parse_time(schedule["closes_at"])
            return {"table_id": str(table_id), "start_at": iso(start), "end_at": iso(end),
                    "available": in_window and not busy and start > now, "busy_intervals": busy}

    def hold(self, data: HoldRequest | dict) -> dict:
        data = HoldRequest.model_validate(data).model_dump(mode="json")
        now = self.ctx.now()
        start, end = validate_interval(data["start_at"], data["end_at"], now=now)
        self._table(data["table_id"])
        with self.ctx.store.transaction() as tx:
            holds = tx.bucket("holds")
            existing = next((h for h in holds.values() if h["booking_id"] == data["booking_id"]), None)
            if existing is not None:
                matches = (existing["table_id"] == data["table_id"]
                           and parse_time(existing["start_at"]) == start and parse_time(existing["end_at"]) == end)
                if matches and self._active(existing, now):
                    return deepcopy(existing)
                raise DomainError(409, "Для этой брони уже существует другой или закрытый резерв")
            schedule = tx.bucket("schedules").get(data["table_id"])
            if schedule is None:
                raise DomainError(409, "Рабочее окно стола не задано")
            if not (parse_time(schedule["opens_at"]) <= start and end <= parse_time(schedule["closes_at"])):
                raise DomainError(409, "Интервал выходит за рабочее окно")
            for hold in holds.values():
                if (hold["table_id"] == data["table_id"] and self._active(hold, now)
                        and self._overlaps(start, end, parse_time(hold["start_at"]), parse_time(hold["end_at"]))):
                    raise DomainError(409, "Интервал уже занят")
            hold = {"id": str(uuid4()), **data, "start_at": iso(start), "end_at": iso(end),
                    "expires_at": iso(min(now + timedelta(minutes=10), start)), "status": "held"}
            holds[hold["id"]] = hold
            return deepcopy(hold)

    def confirm(self, hold_id, booking_id) -> dict:
        with self.ctx.store.transaction() as tx:
            hold = self._owned_hold(tx, hold_id, booking_id)
            if hold["status"] == "confirmed":
                return deepcopy(hold)
            if hold["status"] != "held" or parse_time(hold["expires_at"]) <= self.ctx.now():
                raise DomainError(409, "Резерв больше не действует")
            hold["status"] = "confirmed"
            return deepcopy(hold)

    def release(self, hold_id, booking_id) -> dict:
        with self.ctx.store.transaction() as tx:
            hold = self._owned_hold(tx, hold_id, booking_id)
            if hold["status"] not in {"released", "expired"}:
                hold["status"] = "released"
            return deepcopy(hold)

    @staticmethod
    def _owned_hold(tx, hold_id, booking_id):
        hold = tx.bucket("holds").get(str(hold_id))
        if hold is None:
            raise DomainError(404, "Резерв не найден")
        if hold["booking_id"] != str(booking_id):
            raise DomainError(409, "Резерв принадлежит другой брони")
        return hold

    def tick(self):
        with self.ctx.store.transaction() as tx:
            now = self.ctx.now()
            for hold in tx.bucket("holds").values():
                if hold["status"] == "held" and parse_time(hold["expires_at"]) <= now:
                    hold["status"] = "expired"
                    tx.emit("schedule.hold_expired", {"booking_id": hold["booking_id"], "hold_id": hold["id"]})

    def handle(self, event, tx):
        if event["type"] not in {"booking.cancelled", "booking.expired"}:
            return
        data = event["data"]
        for hold in tx.bucket("holds").values():
            if hold["booking_id"] == data["booking_id"] and (not data.get("hold_id") or hold["id"] == data["hold_id"]):
                if hold["status"] not in {"released", "expired"}:
                    hold["status"] = "released"
