from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Query
from .models import PublishTariff, QuoteRequest, TariffResponse, QuoteResponse


def make_router(service):
    router = APIRouter(prefix="/api", tags=["Тарифы и предложения"])

    @router.post("/tariffs", status_code=201, response_model=TariffResponse, summary="Опубликовать новую неизменяемую версию тарифа")
    def publish(data: PublishTariff):
        return service.publish(data)

    @router.get("/tariffs", response_model=list[TariffResponse], summary="Получить версии тарифов")
    def list_tariffs(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                     game_type: Literal["pool", "russian", "snooker"] | None = None):
        return service.list_tariffs(offset, limit, game_type)

    @router.post("/quotes", status_code=201, response_model=QuoteResponse, summary="Рассчитать стоимость после проверки стола в S2")
    def quote(data: QuoteRequest):
        return service.quote(data)

    @router.post("/quotes/{quote_id}:validate", response_model=QuoteResponse, summary="Проверить срок и применимость ценового предложения")
    def validate(quote_id: UUID, data: QuoteRequest):
        return service.validate_quote(str(quote_id), data)

    return router
