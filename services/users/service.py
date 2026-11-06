"""Данные клиентов; контакт уникален без учёта регистра."""
from copy import deepcopy
from uuid import uuid4

from common.errors import DomainError
from common.time import iso
from .models import CreateUser, UpdateUser


class Service:
    def __init__(self, ctx):
        self.ctx = ctx

    def create(self, data: CreateUser | dict) -> dict:
        data = CreateUser.model_validate(data).model_dump()
        with self.ctx.store.transaction() as tx:
            users = tx.bucket("users")
            self._unique_contact(users, data["contact"])
            user = {"id": str(uuid4()), **data, "created_at": iso(self.ctx.now())}
            users[user["id"]] = user
            return deepcopy(user)

    def get(self, user_id: str) -> dict:
        with self.ctx.store.transaction() as tx:
            user = tx.bucket("users").get(str(user_id))
            if user is None:
                raise DomainError(404, "Пользователь не найден")
            return deepcopy(user)

    def list(self, offset: int = 0, limit: int = 50) -> list[dict]:
        with self.ctx.store.transaction() as tx:
            return deepcopy(list(tx.bucket("users").values())[offset:offset + limit])

    def update(self, user_id: str, data: UpdateUser | dict) -> dict:
        changes = UpdateUser.model_validate(data).model_dump(exclude_unset=True)
        if not changes or any(value is None for value in changes.values()):
            raise DomainError(422, "Передайте хотя бы одно непустое поле профиля")
        with self.ctx.store.transaction() as tx:
            users = tx.bucket("users")
            user = users.get(str(user_id))
            if user is None:
                raise DomainError(404, "Пользователь не найден")
            if "contact" in changes:
                self._unique_contact(users, changes["contact"], str(user_id))
            user.update(changes)
            return deepcopy(user)

    @staticmethod
    def _unique_contact(users: dict, contact: str, current_id: str | None = None):
        if any(u["contact"].casefold() == contact.casefold() and u["id"] != current_id for u in users.values()):
            raise DomainError(409, "Контакт уже зарегистрирован")

    def handle(self, event, tx):
        pass  # S1 не подписывается на события других сервисов.
