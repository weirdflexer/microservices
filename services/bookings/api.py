from .models import BookingResponse
from uuid import UUID

from fastapi import APIRouter, Query

from .models import BookingCreate


def make_router(service):
    router = APIRouter(prefix="/api", tags=["Бронирования"])

    @router.post("/bookings", status_code=201, summary="Оформить бронь и начать ожидание оплаты", response_model=BookingResponse)
    def create(body: BookingCreate):
        return service.create(body.model_dump(mode="json"))

    @router.get("/bookings", summary="Список броней", response_model=list[BookingResponse])
    def list_bookings(user_id: UUID | None = None, status: str | None = None,
                      offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
        return service.list(str(user_id) if user_id else None, status, offset, limit)

    @router.get("/bookings/{booking_id}", summary="Получить состояние и реквизиты оплаты", response_model=BookingResponse)
    def get(booking_id: UUID):
        return service.get(str(booking_id))

    @router.post("/bookings/{booking_id}:cancel", summary="Отменить бронь до начала игры", response_model=BookingResponse)
    def cancel(booking_id: UUID):
        return service.cancel(str(booking_id))

    return router
