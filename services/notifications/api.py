from .models import NotificationResponse
from uuid import UUID

from fastapi import APIRouter, Query

from .models import NotificationCreate


def make_router(service):
    router = APIRouter(prefix="/api", tags=["Уведомления"])

    @router.post("/notifications", status_code=201, summary="Подготовить сообщение или напоминание", response_model=NotificationResponse)
    def create(body: NotificationCreate):
        return service.create(body.model_dump(mode="json"))

    @router.get("/notifications", summary="Список уведомлений", response_model=list[NotificationResponse])
    def list_notifications(user_id: UUID | None = None, offset: int = Query(0, ge=0),
                           limit: int = Query(100, ge=1, le=100)):
        return service.list(str(user_id) if user_id else None, offset, limit)

    @router.get("/notifications/{notification_id}", summary="Состояние доставки", response_model=NotificationResponse)
    def get(notification_id: UUID):
        return service.get(str(notification_id))

    @router.post("/notifications/{notification_id}:send", summary="Отправить через учебный адаптер, получив контакт", response_model=NotificationResponse)
    def send(notification_id: UUID):
        return service.send(str(notification_id))

    return router
