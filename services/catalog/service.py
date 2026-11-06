from copy import deepcopy
from uuid import uuid4
from common.errors import DomainError
from .models import CreateTable, UpdateTable


class Service:
    def __init__(self, ctx):
        self.ctx = ctx

    def create(self, data: CreateTable | dict) -> dict:
        data = CreateTable.model_validate(data).model_dump()
        with self.ctx.store.transaction() as tx:
            tables = tx.bucket("tables")
            self._unique_number(tables, data["number"])
            table = {"id": str(uuid4()), **data}
            tables[table["id"]] = table
            return deepcopy(table)

    def get(self, table_id: str) -> dict:
        with self.ctx.store.transaction() as tx:
            table = tx.bucket("tables").get(str(table_id))
            if table is None:
                raise DomainError(404, "Стол не найден")
            return deepcopy(table)

    def list(self, offset=0, limit=50, published=None, game_type=None) -> list[dict]:
        with self.ctx.store.transaction() as tx:
            tables = [t for t in tx.bucket("tables").values()
                      if (published is None or t["published"] == published)
                      and (game_type is None or t["game_type"] == game_type)]
            return deepcopy(tables[offset:offset + limit])

    def update(self, table_id: str, data: UpdateTable | dict) -> dict:
        changes = UpdateTable.model_validate(data).model_dump(exclude_unset=True)
        if not changes or any(value is None for value in changes.values()):
            raise DomainError(422, "Передайте хотя бы одно непустое поле карточки")
        with self.ctx.store.transaction() as tx:
            tables = tx.bucket("tables")
            table = tables.get(str(table_id))
            if table is None:
                raise DomainError(404, "Стол не найден")
            if "number" in changes:
                self._unique_number(tables, changes["number"], str(table_id))
            table.update(changes)
            return deepcopy(table)

    @staticmethod
    def _unique_number(tables, number, current_id=None):
        if any(t["number"] == number and t["id"] != current_id for t in tables.values()):
            raise DomainError(409, "Номер стола уже занят")

    def handle(self, event, tx):
        pass  # S2 получает изменения только через HTTP.
