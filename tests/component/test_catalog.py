from uuid import uuid4
from fastapi.testclient import TestClient
from common.context import Context
from common.store import MemoryStore
from services.catalog.main import build_app


def test_catalog_http_contract_publication_filter_and_conflict():
    ctx = Context("catalog", MemoryStore("catalog"), None)
    with TestClient(build_app(ctx, background=False)) as client:
        response = client.post("/api/tables", json={"number": 1, "game_type": "pool"})
        assert response.status_code == 201
        table_id = response.json()["id"]
        assert client.get(f"/api/tables/{table_id}").json()["published"] is False
        assert client.get("/api/tables", params={"published": True}).json() == []
        assert client.patch(f"/api/tables/{table_id}", json={"published": True}).status_code == 200
        assert len(client.get("/api/tables", params={"published": True, "game_type": "pool"}).json()) == 1
        assert client.post("/api/tables", json={"number": 1, "game_type": "snooker"}).status_code == 409
        assert client.post("/api/tables", json={"number": -1, "game_type": "pool"}).status_code == 422
        assert client.get(f"/api/tables/{uuid4()}").status_code == 404
