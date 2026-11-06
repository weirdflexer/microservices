"""Сквозной контрактный тест: 8 ASGI приложений, часы и доставка событий в памяти.

Это дополнительная проверка связности; она не выдаётся за тест реального брокера.
"""
import importlib
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from common.context import Context
from common.errors import DomainError
from common.messaging import BINDINGS, process_incoming
from common.settings import Settings
from common.store import MemoryStore
from common.time import iso

NAMES = ("users", "catalog", "schedule", "bookings", "pricing", "payments", "sessions", "notifications")


class Cluster:
    def __init__(self):
        self.time = datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
        self.contexts = {}
        self.clients = {}
        self.services = {}
        self.stack = ExitStack()
        for name in NAMES:
            ctx = Context(name, MemoryStore(name), self, now=lambda: self.time, settings=Settings())
            self.contexts[name] = ctx
            module = importlib.import_module(f"services.{name}.main")
            app = module.build_app(context=ctx, background=False)
            self.clients[name] = self.stack.enter_context(TestClient(app))
            self.services[name] = app.state.service

    def request(self, service, method, path, json=None):
        result = self.clients[service].request(method, path, json=json, headers={"X-Provider-Token": "demo-provider-token"})
        if result.is_error:
            raise DomainError(result.status_code, result.text)
        return result.json() if result.content else None

    def drain(self):
        for _ in range(30):
            delivered = 0
            for name, ctx in self.contexts.items():
                with ctx.store.transaction() as tx:
                    events = deepcopy(tx.outbox)
                    tx.outbox.clear()
                for event in events:
                    for destination, keys in BINDINGS.items():
                        if event["type"] in keys:
                            assert process_incoming(self.services[destination], self.contexts[destination], event)
                            # At-least-once replay must cause no second side effect.
                            assert not process_incoming(self.services[destination], self.contexts[destination], event)
                            delivered += 1
            if not delivered:
                return
        raise AssertionError("Event loop did not settle")

    def prepare(self):
        user = self.request("users", "POST", "/api/users", {"name": "Тест", "contact": "test@example.test"})
        table = self.request("catalog", "POST", "/api/tables", {"number": 1, "game_type": "pool", "published": True})
        self.request("pricing", "POST", "/api/tariffs", {"game_type": "pool", "hourly_rate": 60000})
        self.request("schedule", "PUT", f"/api/tables/{table['id']}/schedule", {"opens_at": iso(self.time), "closes_at": iso(self.time + timedelta(hours=12))})
        interval = {"table_id": table["id"], "start_at": iso(self.time + timedelta(hours=4)), "end_at": iso(self.time + timedelta(hours=6))}
        quote = self.request("pricing", "POST", "/api/quotes", interval)
        booking = self.request("bookings", "POST", "/api/bookings", {"user_id": user["id"], "quote_id": quote["id"], **interval})
        self.drain()
        return self.request("bookings", "GET", f"/api/bookings/{booking['id']}")

    def pay(self, booking):
        self.request("payments", "POST", f"/api/payments/{booking['payment_id']}:result", {"amount": booking["amount"], "currency": "RUB", "status": "succeeded"})
        self.drain()


@pytest.fixture
def cluster():
    cluster = Cluster()
    try:
        yield cluster
    finally:
        cluster.stack.close()


def test_booking_payment_visit_completion(cluster):
    booking = cluster.prepare()
    cluster.pay(booking)
    assert cluster.request("bookings", "GET", f"/api/bookings/{booking['id']}")["status"] == "confirmed"
    visits = cluster.request("sessions", "GET", "/api/sessions")
    assert len(visits) == 1
    notices = cluster.request("notifications", "GET", "/api/notifications")
    assert {n["kind"] for n in notices} == {"confirmation", "reminder"}
    cluster.services["notifications"].tick()
    cluster.time += timedelta(hours=4)
    cluster.request("sessions", "POST", f"/api/sessions/{visits[0]['id']}:start")
    cluster.time += timedelta(hours=1)
    cluster.request("sessions", "POST", f"/api/sessions/{visits[0]['id']}:complete")
    cluster.drain()
    assert cluster.request("bookings", "GET", f"/api/bookings/{booking['id']}")["status"] == "completed"
    # Early completion does not release the paid interval.
    hold = next(iter(cluster.contexts["schedule"].store.snapshot()["data"]["holds"].values()))
    assert hold["status"] == "confirmed"


def test_cancel_refund_and_reminder_cancellation(cluster):
    booking = cluster.prepare()
    cluster.pay(booking)
    cluster.request("bookings", "POST", f"/api/bookings/{booking['id']}:cancel")
    cluster.drain()
    payment_path = f"/api/payments/{booking['payment_id']}"
    assert cluster.request("payments", "GET", payment_path)["refund_status"] == "pending"
    cluster.request("payments", "POST", payment_path + ":refund-result", {"status": "succeeded"})
    cluster.drain()
    assert cluster.request("bookings", "GET", f"/api/bookings/{booking['id']}")["refund_status"] == "succeeded"
    assert cluster.request("sessions", "GET", "/api/sessions")[0]["status"] == "cancelled"
    notices = cluster.request("notifications", "GET", "/api/notifications")
    assert next(n for n in notices if n["kind"] == "reminder")["status"] == "cancelled"
    assert any(n["kind"] == "refund" for n in notices)


def test_late_payment_after_expiration_compensated(cluster):
    booking = cluster.prepare()
    cluster.time += timedelta(minutes=10)
    cluster.services["schedule"].tick()
    cluster.drain()
    cluster.pay(booking)
    current = cluster.request("bookings", "GET", f"/api/bookings/{booking['id']}")
    assert current["status"] == "expired"
    assert current["refund_status"] == "pending"
    assert cluster.request("sessions", "GET", "/api/sessions") == []


def test_no_show_closes_booking_without_refund(cluster):
    booking = cluster.prepare()
    cluster.pay(booking)
    cluster.time += timedelta(hours=4, minutes=15)
    cluster.services["sessions"].tick()
    cluster.drain()
    current = cluster.request("bookings", "GET", f"/api/bookings/{booking['id']}")
    assert current["status"] == "no_show"
    assert current["refund_status"] is None
