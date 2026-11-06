from uuid import UUID
from fastapi import APIRouter, Query
from .models import CreateUser, UpdateUser, UserResponse


def make_router(service):
    router = APIRouter(prefix="/api", tags=["Пользователи"])

    @router.post("/users", status_code=201, response_model=UserResponse, summary="Зарегистрировать клиента")
    def create(data: CreateUser):
        return service.create(data)

    @router.get("/users", response_model=list[UserResponse], summary="Получить страницу клиентов")
    def list_users(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        return service.list(offset, limit)

    @router.get("/users/{user_id}", response_model=UserResponse, summary="Получить данные и контакт клиента")
    def get(user_id: UUID):
        return service.get(str(user_id))

    @router.patch("/users/{user_id}", response_model=UserResponse, summary="Изменить имя или контакт")
    def update(user_id: UUID, data: UpdateUser):
        return service.update(str(user_id), data)

    return router
