"""Версии тарифов и неизменяемые предложения с десятиминутным сроком."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from common.errors import DomainError
from common.time import iso, parse_time, validate_interval
from .models import PublishTariff, QuoteRequest


class Service:
    def __init__(self, ctx):
        self.ctx = ctx

    def publish(self, data: PublishTariff | dict) -> dict:
        data = PublishTariff.model_validate(data).model_dump()
        with self.ctx.store.transaction() as tx:
            tariffs = tx.bucket("tariffs")
            version = 1 + max((t["version"] for t in tariffs.values() if t["game_type"] == data["game_type"]), default=0)
            tariff = {"id": str(uuid4()), **data, "version": version,
                      "currency": "RUB", "published_at": iso(self.ctx.now())}
            tariffs[tariff["id"]] = tariff
            return deepcopy(tariff)

    def list_tariffs(self, offset=0, limit=50, game_type=None) -> list[dict]:
        with self.ctx.store.transaction() as tx:
            tariffs = [t for t in tx.bucket("tariffs").values()
                       if game_type is None or t["game_type"] == game_type]
            return deepcopy(tariffs[offset:offset + limit])

    def quote(self, data: QuoteRequest | dict) -> dict:
        data = QuoteRequest.model_validate(data).model_dump(mode="json")
        now = self.ctx.now()
        start, end = validate_interval(data["start_at"], data["end_at"], now=now)
        table = self.ctx.http.request("catalog", "GET", f'/api/tables/{data["table_id"]}')
        if not table["published"]:
            raise DomainError(409, "Стол не опубликован")
        with self.ctx.store.transaction() as tx:
            matches = [t for t in tx.bucket("tariffs").values() if t["game_type"] == table["game_type"]]
            if not matches:
                raise DomainError(409, "Тариф для этого вида игры не опубликован")
            tariff = max(matches, key=lambda t: t["version"])
            minutes = int((end - start).total_seconds() // 60)
            # Округление вверх до копейки, без ошибок float.
            amount = (tariff["hourly_rate"] * minutes + 59) // 60
            quote = {"id": str(uuid4()), "table_id": data["table_id"],
                     "start_at": iso(start), "end_at": iso(end), "amount": amount,
                     "currency": "RUB", "expires_at": iso(now + timedelta(minutes=10)),
                     "tariff_id": tariff["id"], "tariff_version": tariff["version"],
                     "hourly_rate": tariff["hourly_rate"]}
            tx.bucket("quotes")[quote["id"]] = quote
            return deepcopy(quote)

    def validate_quote(self, quote_id: str, data: QuoteRequest | dict) -> dict:
        data = QuoteRequest.model_validate(data).model_dump(mode="json")
        now = self.ctx.now()
        start, end = validate_interval(data["start_at"], data["end_at"], now=now)
        with self.ctx.store.transaction() as tx:
            quote = tx.bucket("quotes").get(str(quote_id))
            if quote is None:
                raise DomainError(404, "Ценовое предложение не найдено")
            if parse_time(quote["expires_at"]) <= now:
                raise DomainError(409, "Срок ценового предложения истёк")
            if (quote["table_id"] != data["table_id"] or parse_time(quote["start_at"]) != start
                    or parse_time(quote["end_at"]) != end):
                raise DomainError(409, "Предложение относится к другому столу или интервалу")
            return deepcopy(quote)

    def handle(self, event, tx):
        pass  # S5 получает запросы синхронно.
