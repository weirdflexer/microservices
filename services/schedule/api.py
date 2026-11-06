from uuid import UUID
from fastapi import APIRouter, Query
from pydantic import AwareDatetime
from .models import ScheduleRequest, HoldRequest, ConfirmRequest, ScheduleResponse, HoldResponse, AvailabilityResponse


def make_router(service):
    router = APIRouter(prefix="/api", tags=["Расписание"])

    @router.put("/tables/{table_id}/schedule", response_model=ScheduleResponse, summary="Задать рабочее окно с проверкой стола в S2")
    def set_schedule(table_id: UUID, data: ScheduleRequest):
        return service.set_schedule(str(table_id), data)

    @router.get("/tables/{table_id}/availability", response_model=AvailabilityResponse, summary="Проверить доступность интервала")
    def availability(table_id: UUID, start_at: AwareDatetime = Query(), end_at: AwareDatetime = Query()):
        return service.availability(str(table_id), start_at.isoformat(), end_at.isoformat())

    @router.post("/holds", status_code=201, response_model=HoldResponse, summary="Удержать свободный интервал на срок до 10 минут")
    def hold(data: HoldRequest):
        return service.hold(data)

    @router.post("/holds/{hold_id}:confirm", response_model=HoldResponse, summary="Закрепить действующий резерв за его бронью")
    def confirm(hold_id: UUID, data: ConfirmRequest):
        return service.confirm(str(hold_id), str(data.booking_id))

    @router.delete("/holds/{hold_id}", response_model=HoldResponse, summary="Освободить резерв или закреплённый интервал")
    def release(hold_id: UUID, booking_id: UUID = Query(description="Идентификатор владельца резерва")):
        return service.release(str(hold_id), str(booking_id))

    return router
