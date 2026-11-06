from datetime import datetime, timezone
from uuid import uuid4
from fastapi.testclient import TestClient
from common.context import Context
from common.store import MemoryStore
from services.users.main import build_app


def test_user_http_contract_crud_and_validation():
    ctx = Context("users", MemoryStore("users"), None, now=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    with TestClient(build_app(ctx, background=False)) as client:
        response = client.post("/api/users", json={"name": "Данил", "contact": "dan@example.test"})
        assert response.status_code == 201
        user_id = response.json()["id"]
        assert client.get(f"/api/users/{user_id}").json()["name"] == "Данил"
        assert client.patch(f"/api/users/{user_id}", json={"name": "Данек"}).json()["name"] == "Данек"
        assert len(client.get("/api/users", params={"offset": 0, "limit": 1}).json()) == 1
        assert client.post("/api/users", json={"name": "Другой", "contact": "DAN@example.test"}).status_code == 409
        assert client.post("/api/users", json={"name": "Без контакта"}).status_code == 422
        assert client.get(f"/api/users/{uuid4()}").status_code == 404
        assert client.get("/api/users/not-a-uuid").status_code == 422
        assert client.get("/api/users", params={"limit": 101}).status_code == 422
        assert client.patch(f"/api/users/{user_id}", json={"name": None}).status_code == 422
