"""HTTP smoke-сценарий реального стенда: оформление, оплата, отмена, возврат."""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

NAMES = ("users", "catalog", "schedule", "bookings", "pricing", "payments", "sessions", "notifications")
PORTS = dict(zip(NAMES, range(8001, 8009)))


def wait_for(fetch, predicate, timeout=45):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fetch()
        if predicate(last):
            return last
        time.sleep(0.3)
    raise RuntimeError(f"Ожидаемое состояние не наступило за {timeout} с. Последний ответ: {last}")


def main():
    with httpx.Client(timeout=10, trust_env=False) as client:
        def request(service, method, path, data=None):
            response = client.request(method, f"http://localhost:{PORTS[service]}{path}", json=data,
                                      headers={"X-Provider-Token": os.getenv("PROVIDER_TOKEN", "demo-provider-token")})
            if response.is_error:
                raise RuntimeError(f"{service} {method} {path}: {response.status_code} {response.text}")
            return response.json() if response.content else None

        def probe(name):
            try:
                return request(name, "GET", "/health")
            except httpx.RequestError:
                return {"status": "starting"}

        for name in NAMES:
            wait_for(lambda: probe(name), lambda row: row.get("status") == "ok" and row.get("broker_connected"), timeout=90)
        now = datetime.now(timezone.utc)
        start = (now + timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=2)
        interval = {"start_at": start.isoformat(), "end_at": end.isoformat()}
        user = request("users", "POST", "/api/users", {"name": "Демо-клиент", "contact": f"{uuid4().hex}@example.test"})
        table = request("catalog", "POST", "/api/tables", {"number": int(time.time_ns() % 1_000_000_000), "game_type": "pool", "description": "Практики 5–8", "published": True})
        request("pricing", "POST", "/api/tariffs", {"game_type": "pool", "hourly_rate": 60000})
        request("schedule", "PUT", f"/api/tables/{table['id']}/schedule", {
            "opens_at": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "closes_at": (end + timedelta(hours=4)).isoformat()})
        quote = request("pricing", "POST", "/api/quotes", {"table_id": table["id"], **interval})
        booking = request("bookings", "POST", "/api/bookings", {"user_id": user["id"], "table_id": table["id"], "quote_id": quote["id"], **interval})
        booking_path = f"/api/bookings/{booking['id']}"
        booking = wait_for(lambda: request("bookings", "GET", booking_path), lambda row: row["payment_id"] is not None)
        payment_path = f"/api/payments/{booking['payment_id']}"
        request("payments", "POST", payment_path + ":result", {"status": "succeeded", "amount": quote["amount"], "currency": "RUB"})
        booking = wait_for(lambda: request("bookings", "GET", booking_path), lambda row: row["status"] == "confirmed")
        visits = wait_for(lambda: request("sessions", "GET", f"/api/sessions?booking_id={booking['id']}"), bool)
        notifications = wait_for(lambda: request("notifications", "GET", f"/api/notifications?user_id={user['id']}"), bool)
        confirmation = next(n for n in notifications if n["kind"] == "confirmation")
        request("notifications", "POST", f"/api/notifications/{confirmation['id']}:send")
        request("bookings", "POST", booking_path + ":cancel")
        wait_for(lambda: request("payments", "GET", payment_path), lambda row: row["refund_status"] == "pending")
        request("payments", "POST", payment_path + ":refund-result", {"status": "succeeded"})
        booking = wait_for(lambda: request("bookings", "GET", booking_path), lambda row: row["refund_status"] == "succeeded")
        visit = wait_for(lambda: request("sessions", "GET", f"/api/sessions/{visits[0]['id']}"), lambda row: row["status"] == "cancelled")
        availability = wait_for(lambda: client.get(f"http://localhost:8003/api/tables/{table['id']}/availability", params=interval).json(), lambda row: row.get("available") is True)
        print(json.dumps({"result": "PASS", "booking_id": booking["id"], "payment_id": booking["payment_id"],
                          "booking_status": booking["status"], "refund_status": booking["refund_status"],
                          "visit_status": visit["status"], "interval_available": availability["available"],
                          "amount_kopecks": booking["amount"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
