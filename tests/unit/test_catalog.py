from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from common.errors import DomainError
from common.store import MemoryStore
from services.catalog import Service


def make_service():
    return Service(SimpleNamespace(store=MemoryStore("catalog")))


def test_unique_table_numbers_and_publication():
    service = make_service()
    first = service.create({"number": 1, "game_type": "pool"})
    second = service.create({"number": 2, "game_type": "snooker"})
    with pytest.raises(DomainError):
        service.create({"number": 1, "game_type": "russian"})
    with pytest.raises(DomainError):
        service.update(second["id"], {"number": 1})
    assert service.list(published=True) == []
    service.update(first["id"], {"published": True})
    assert [t["id"] for t in service.list(published=True)] == [first["id"]]
    assert service.list(game_type="snooker")[0]["id"] == second["id"]


@pytest.mark.parametrize("data", [{"number": 0, "game_type": "pool"}, {"number": 1, "game_type": "chess"}])
def test_table_domain_validation(data):
    with pytest.raises(ValidationError):
        make_service().create(data)


def test_patch_does_not_allow_null_or_no_changes():
    service = make_service()
    table = service.create({"number": 1, "game_type": "pool"})
    for changes in ({}, {"published": None}):
        with pytest.raises(DomainError):
            service.update(table["id"], changes)
