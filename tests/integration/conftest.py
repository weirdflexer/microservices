"""Language-neutral client for the real eight-service HTTP/AMQP stack.

Opt in with RUN_HTTP_INTEGRATION=1. No imports from services or common are used.
Every test creates isolated UUID/contact/table data; no database reset is performed.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest


NAMES = ("users", "catalog", "schedule", "bookings", "pricing", "payments", "sessions", "notifications")


class HTTPStack:
    def __init__(self):
        self.urls = {
            name: os.environ.get(f"{name.upper()}_URL", f"http://127.0.0.1:{8001 + index}").rstrip("/")
            for index, name in enumerate(NAMES)
        }
        self.client = httpx.Client(timeout=float(os.environ.get("HTTP_TEST_REQUEST_TIMEOUT", "20")), trust_env=False)
        self.token = os.environ.get("PROVIDER_TOKEN", "demo-provider-token")
        self.wait_timeout = float(os.environ.get("HTTP_TEST_WAIT_TIMEOUT", "45"))

    def raw(self, service, method, path, *, body=None, params=None, headers=None, content=None):
        arguments = {"params": params, "headers": headers}
        if content is not None:
            arguments["content"] = content
        elif body is not None:
            arguments["json"] = body
        return self.client.request(method, self.urls[service] + path, **arguments)

    def request(self, service, method, path, *, body=None, params=None, status=200, provider=False):
        headers = {"X-Provider-Token": self.token} if provider else None
        response = self.raw(service, method, path, body=body, params=params, headers=headers)
        assert response.status_code == status, (
            f"{service} {method} {path}: expected {status}, got "
            f"{response.status_code}; {response.text[:1600]}"
        )
        assert "application/json" in response.headers.get("content-type", ""), response.headers
        return response.json()

    def wait(self, fetch, predicate, *, description="expected asynchronous state", timeout=None):
        deadline = time.monotonic() + (self.wait_timeout if timeout is None else timeout)
        last = None
        while time.monotonic() < deadline:
            last = fetch()
            if predicate(last):
                return last
            time.sleep(0.15)
        pytest.fail(f"Timeout waiting for {description}; last response: {last}")

    def ready(self):
        def probe():
            states = {}
            for name in NAMES:
                try:
                    response = self.raw(name, "GET", "/health")
                    states[name] = response.json() if response.status_code == 200 else {"status": response.status_code}
                except (httpx.RequestError, ValueError) as exc:
                    states[name] = {"status": type(exc).__name__}
            return states

        return self.wait(
            probe,
            lambda states: all(row.get("status") == "ok" and row.get("broker_connected") is True for row in states.values()),
            description="all eight HTTP applications and RabbitMQ connections",
            timeout=float(os.environ.get("HTTP_TEST_STARTUP_TIMEOUT", "90")),
        )

    def prepare(self, *, hours_ahead=4, duration_hours=2):
        now = datetime.now(timezone.utc)
        start = (now + timedelta(hours=hours_ahead)).replace(minute=0, second=0, microsecond=0)
        if start <= now:
            start += timedelta(hours=1)
        end = start + timedelta(hours=duration_hours)
        interval = {"start_at": start.isoformat(), "end_at": end.isoformat()}
        user = self.request("users", "POST", "/api/users", status=201, body={
            "name": "HTTP contract test", "contact": f"contract-{uuid4().hex}@example.test"})
        table = self.request("catalog", "POST", "/api/tables", status=201, body={
            "number": uuid4().int % 2_000_000_000 + 1,
            "game_type": "pool", "published": True, "description": "Isolated HTTP integration fixture"})
        tariff = self.request("pricing", "POST", "/api/tariffs", status=201, body={
            "game_type": "pool", "hourly_rate": 60000})
        schedule = self.request("schedule", "PUT", f"/api/tables/{table['id']}/schedule", body={
            "opens_at": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "closes_at": (end + timedelta(hours=4)).isoformat()})
        quote = self.request("pricing", "POST", "/api/quotes", status=201, body={
            "table_id": table["id"], **interval})
        return {"user": user, "table": table, "tariff": tariff, "schedule": schedule,
                "quote": quote, "interval": interval}

    @staticmethod
    def booking_body(prepared):
        return {"user_id": prepared["user"]["id"], "table_id": prepared["table"]["id"],
                "quote_id": prepared["quote"]["id"], **prepared["interval"]}

    def book(self, prepared):
        booking = self.request("bookings", "POST", "/api/bookings", status=201, body=self.booking_body(prepared))
        assert booking["status"] == "waiting_payment"
        assert booking["amount"] == prepared["quote"]["amount"]
        return self.wait(
            lambda: self.booking(booking["id"]), lambda row: row.get("payment_id") is not None,
            description="Go payment creation and payment.created delivery to Go bookings")

    def booking(self, booking_id):
        return self.request("bookings", "GET", f"/api/bookings/{booking_id}")

    def payment(self, payment_id):
        return self.request("payments", "GET", f"/api/payments/{payment_id}")

    def callback(self, booking, outcome="succeeded", amount=None):
        return self.request("payments", "POST", f"/api/payments/{booking['payment_id']}:result",
                            provider=True, body={"status": outcome,
                            "amount": booking["amount"] if amount is None else amount, "currency": "RUB"})

    def pay(self, booking):
        self.callback(booking)
        return self.wait(lambda: self.booking(booking["id"]), lambda row: row["status"] == "confirmed",
                         description="payment.succeeded, Node hold confirmation and booking.confirmed")

    def visits(self, booking_id):
        return self.request("sessions", "GET", "/api/sessions", params={"booking_id": booking_id})

    def notices(self, user_id):
        return self.request("notifications", "GET", "/api/notifications", params={"user_id": user_id})

    def availability(self, prepared):
        return self.request("schedule", "GET", f"/api/tables/{prepared['table']['id']}/availability",
                            params=prepared["interval"])


@pytest.fixture(scope="session")
def live_stack():
    if os.environ.get("RUN_HTTP_INTEGRATION") != "1":
        pytest.skip("Set RUN_HTTP_INTEGRATION=1 to test the actual running eight-service stack")
    stack = HTTPStack()
    try:
        stack.ready()
        yield stack
    finally:
        stack.client.close()


@pytest.fixture(scope="module")
def prepared(live_stack):
    return live_stack.prepare()
