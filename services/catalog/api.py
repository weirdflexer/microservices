from uuid import UUID
from fastapi import APIRouter, Query
from .models import CreateTable, UpdateTable, GameType, TableResponse


def make_router(service):
    router = APIRouter(prefix="/api", tags=["Каталог столов"])

    @router.post("/tables", status_code=201, response_model=TableResponse, summary="Добавить стол")
    def create(data: CreateTable):
        return service.create(data)

    @router.get("/tables", response_model=list[TableResponse], summary="Найти столы по виду игры и публикации")
    def list_tables(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                    published: bool | None = None, game_type: GameType | None = None):
        return service.list(offset, limit, published, game_type)

    @router.get("/tables/{table_id}", response_model=TableResponse, summary="Получить сведения о столе")
    def get(table_id: UUID):
        return service.get(str(table_id))

    @router.patch("/tables/{table_id}", response_model=TableResponse, summary="Изменить карточку или опубликовать стол")
    def update(table_id: UUID, data: UpdateTable):
        return service.update(str(table_id), data)

    return router
