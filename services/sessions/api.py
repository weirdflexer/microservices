from .models import SessionResponse
from uuid import UUID

from fastapi import APIRouter, Query


def make_router(service):
    router = APIRouter(prefix="/api", tags=["Игровые сеансы"])

    @router.get("/sessions", summary="Список посещений", response_model=list[SessionResponse])
    def list_sessions(booking_id: UUID | None = None, offset: int = Query(0, ge=0),
                      limit: int = Query(100, ge=1, le=100)):
        return service.list(str(booking_id) if booking_id else None, offset, limit)

    @router.get("/sessions/{session_id}", summary="Получить посещение", response_model=SessionResponse)
    def get(session_id: UUID):
        return service.get(str(session_id))

    @router.post("/sessions/{session_id}:start", summary="Начать игру, проверив бронь", response_model=SessionResponse)
    def start(session_id: UUID):
        return service.start(str(session_id))

    @router.post("/sessions/{session_id}:complete", summary="Завершить начатый сеанс", response_model=SessionResponse)
    def complete(session_id: UUID):
        return service.complete(str(session_id))

    return router
