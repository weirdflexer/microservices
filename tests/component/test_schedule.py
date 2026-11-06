from datetime import datetime, timezone
from uuid import uuid4
from fastapi.testclient import TestClient
from common.context import Context
from common.store import MemoryStore
from services.schedule.main import build_app


class FakeCatalog:
    def __init__(self):
        self.calls = []

    def request(self, service, method, path, json=None):
        self.calls.append((service, method, path))
        return {"published": True}


def test_schedule_http_contract_overlap_confirm_and_release():
    http = FakeCatalog()
    ctx = Context("schedule", MemoryStore("schedule"), http,
                  now=lambda: datetime(2030, 1, 1, 8, tzinfo=timezone.utc))
    table_id, booking_id = str(uuid4()), str(uuid4())
    data = {"table_id": table_id, "booking_id": booking_id,
            "start_at": "2030-01-01T12:00:00+00:00", "end_at": "2030-01-01T13:00:00+00:00"}
    interval = {"start_at": data["start_at"], "end_at": data["end_at"]}
    with TestClient(build_app(ctx, background=False)) as client:
        assert client.put(f"/api/tables/{table_id}/schedule", json={"opens_at": "2030-01-01T09:00:00+00:00", "closes_at": "2030-01-01T23:00:00+00:00"}).status_code == 200
        assert client.get(f"/api/tables/{table_id}/availability", params=interval).json()["available"]
        response = client.post("/api/holds", json=data)
        assert response.status_code == 201
        hold_id = response.json()["id"]
        assert client.post("/api/holds", json={**data, "booking_id": str(uuid4())}).status_code == 409
        assert client.post(f"/api/holds/{hold_id}:confirm", json={"booking_id": booking_id}).json()["status"] == "confirmed"
        assert not client.get(f"/api/tables/{table_id}/availability", params=interval).json()["available"]
        assert client.delete(f"/api/holds/{hold_id}", params={"booking_id": str(uuid4())}).status_code == 409
        assert client.delete(f"/api/holds/{hold_id}", params={"booking_id": booking_id}).json()["status"] == "released"
        assert client.get(f"/api/tables/{table_id}/availability", params=interval).json()["available"]
        assert http.calls and all(call == ("catalog", "GET", f"/api/tables/{table_id}") for call in http.calls)
