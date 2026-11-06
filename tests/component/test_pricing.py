from datetime import datetime, timedelta, timezone
from uuid import uuid4
from fastapi.testclient import TestClient
from common.context import Context
from common.errors import DomainError
from common.store import MemoryStore
from services.pricing.main import build_app


class FakeCatalog:
    def __init__(self):
        self.calls = []
        self.failure = False

    def request(self, service, method, path, json=None):
        self.calls.append((service, method, path))
        if self.failure:
            raise DomainError(503, "Каталог недоступен")
        return {"published": True, "game_type": "pool"}


def test_pricing_http_contract_and_outgoing_catalog_request():
    clock = [datetime(2030, 1, 1, 8, tzinfo=timezone.utc)]
    http = FakeCatalog()
    ctx = Context("pricing", MemoryStore("pricing"), http, now=lambda: clock[0])
    data = {"table_id": str(uuid4()), "start_at": "2030-01-01T12:00:00+00:00", "end_at": "2030-01-01T14:00:00+00:00"}
    with TestClient(build_app(ctx, background=False)) as client:
        assert client.post("/api/tariffs", json={"game_type": "pool", "hourly_rate": 10000}).status_code == 201
        response = client.post("/api/quotes", json=data)
        assert response.status_code == 201
        quote = response.json()
        assert quote["amount"] == 20000
        assert http.calls == [("catalog", "GET", f'/api/tables/{data["table_id"]}')]
        assert client.post(f'/api/quotes/{quote["id"]}:validate', json=data).status_code == 200
        assert len(client.get("/api/tariffs").json()) == 1
        clock[0] += timedelta(minutes=10)
        assert client.post(f'/api/quotes/{quote["id"]}:validate', json=data).status_code == 409
        assert client.post("/api/quotes", json={**data, "start_at": "2030-01-01T12:00:00"}).status_code == 422
        assert client.post("/api/tariffs", json={"game_type": "pool", "hourly_rate": -1}).status_code == 422
        http.failure = True
        assert client.post("/api/quotes", json=data).status_code == 503
