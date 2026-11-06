from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from common.errors import DomainError
from common.store import MemoryStore
from services.users import Service


def make_service():
    ctx = SimpleNamespace(store=MemoryStore("users"), now=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    return Service(ctx)


def test_contact_uniqueness_is_case_insensitive_and_patch_atomic():
    service = make_service()
    first = service.create({"name": "Данил", "contact": "dan@example.test"})
    second = service.create({"name": "Саша", "contact": "sasha@example.test"})
    with pytest.raises(DomainError):
        service.create({"name": "Дубликат", "contact": "DAN@example.test"})
    with pytest.raises(DomainError):
        service.update(second["id"], {"name": "Изменено", "contact": "dan@example.test"})
    assert service.get(second["id"])["name"] == "Саша"
    assert service.update(first["id"], {"contact": "DAN@example.test"})["contact"] == "DAN@example.test"


def test_read_returns_detached_value_and_pagination():
    service = make_service()
    user = service.create({"name": "Данил", "contact": "dan@example.test"})
    user["name"] = "Внешняя правка"
    service.create({"name": "Андрей", "contact": "andrey@example.test"})
    assert service.get(user["id"])["name"] == "Данил"
    assert service.list(offset=1, limit=1)[0]["name"] == "Андрей"
    with pytest.raises(DomainError):
        service.get(str(uuid4()))


@pytest.mark.parametrize("data", [{"name": " ", "contact": "abc"}, {"name": "Имя"}])
def test_invalid_profile_rejected(data):
    with pytest.raises(ValidationError):
        make_service().create(data)


def test_empty_or_null_patch_rejected():
    service = make_service()
    user = service.create({"name": "Данил", "contact": "dan@example.test"})
    for changes in ({}, {"name": None}):
        with pytest.raises(DomainError):
            service.update(user["id"], changes)
