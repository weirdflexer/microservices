"""Отказы транспорта проверяются без брокера, БД и сетевых обращений."""
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pika
import pytest

from common.context import Context
from common.errors import DomainError
from common.messaging import BrokerWorker, DLX, EXCHANGE, process_incoming
from common.store import MemoryStore


def envelope(**changes):
    event = {"id": str(uuid4()), "type": "payment.succeeded", "occurred_at": "2030-01-01T08:00:00+00:00",
             "producer": "payments", "correlation_id": str(uuid4()), "data": {"amount": 10000}}
    event.update(changes)
    return event


class CountingService:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = 0

    def handle(self, event, tx):
        self.calls += 1
        tx.bucket("totals")["processed"] = tx.bucket("totals").get("processed", 0) + 1
        tx.emit("booking.confirmed", {"source_id": event["id"]})
        if self.failure is not None:
            raise self.failure


class FakeChannel:
    """basic_publish возвращается только после успешного publisher confirm."""
    def __init__(self, fail_publish_at=None, fail_ack=False):
        self.fail_publish_at = fail_publish_at
        self.fail_ack = fail_ack
        self.published = []
        self.acknowledged = []
        self.rejected = []
        self.operations = []

    def basic_publish(self, **kwargs):
        self.operations.append(("publish", kwargs))
        if len(self.operations) == self.fail_publish_at:
            raise pika.exceptions.NackError([])
        self.published.append(kwargs)

    def basic_ack(self, **kwargs):
        self.operations.append(("ack", kwargs))
        if self.fail_ack:
            raise pika.exceptions.ConnectionClosedByBroker(320, "connection lost before ack")
        self.acknowledged.append(kwargs)

    def basic_nack(self, **kwargs):
        self.rejected.append(kwargs)


def setup_worker(service=None):
    ctx = Context("bookings", MemoryStore("bookings"), None)
    service = service or CountingService()
    return BrokerWorker(ctx, service), ctx, service


def delivery(key="payment.succeeded", exchange=EXCHANGE, tag=7):
    return SimpleNamespace(routing_key=key, exchange=exchange, delivery_tag=tag)


def body(event=None):
    return json.dumps(event or envelope()).encode()


def test_transaction_rolls_back_business_changes_outbox_and_inbox_together():
    store = MemoryStore("bookings")
    with store.transaction() as tx:
        tx.bucket("bookings")["first"] = {"status": "waiting_payment"}
    before = store.snapshot()
    with pytest.raises(RuntimeError):
        with store.transaction() as tx:
            tx.bucket("bookings")["first"]["status"] = "confirmed"
            tx.emit("booking.confirmed", {"booking_id": "first"})
            tx.inbox[str(uuid4())] = "processed"
            raise RuntimeError("crash before commit")
    assert store.snapshot() == before


def test_committed_state_and_emitted_payload_are_detached_from_callers():
    store = MemoryStore("bookings")
    data = {"nested": {"value": 1}}
    with store.transaction() as tx:
        tx.bucket("values")["first"] = data
        sent = tx.emit("booking.confirmed", data)
        data["nested"]["value"] = 2
        assert sent["data"]["nested"]["value"] == 1
    data["nested"]["value"] = 100
    sent["data"]["nested"]["value"] = 100
    snapshot = store.snapshot()
    assert snapshot["data"]["values"]["first"]["nested"]["value"] == 2
    assert snapshot["outbox"][0]["data"]["nested"]["value"] == 1
    snapshot["outbox"].clear()
    assert len(store.snapshot()["outbox"]) == 1


def test_inbox_prevents_duplicate_processing_even_with_uuid_case_changes():
    worker, ctx, service = setup_worker()
    event = envelope()
    assert process_incoming(service, ctx, event) is True
    duplicate = {**event, "id": event["id"].upper()}
    assert process_incoming(service, ctx, duplicate) is False
    assert service.calls == 1
    state = ctx.store.snapshot()
    assert state["data"]["totals"]["processed"] == 1
    assert len(state["outbox"]) == 1
    assert list(state["inbox"]) == [event["id"]]


def test_failure_in_handler_is_retryable_without_partial_commit():
    service = CountingService(failure=DomainError(503, "schedule unavailable"))
    worker, ctx, service = setup_worker(service)
    event = envelope()
    before = ctx.store.snapshot()
    with pytest.raises(DomainError):
        process_incoming(service, ctx, event)
    assert ctx.store.snapshot() == before
    service.failure = None
    assert process_incoming(service, ctx, event)
    assert ctx.store.snapshot()["data"]["totals"]["processed"] == 1


@pytest.mark.parametrize("bad", [None, [], "event", 123, {},
    envelope(id=3), envelope(id="not-a-uuid"), envelope(data=[]), envelope(type=""),
    envelope(occurred_at="2030-01-01T08:00:00"), envelope(occurred_at=1),
    envelope(producer=None), envelope(correlation_id=[]),
    {k: v for k, v in envelope().items() if k != "producer"}])
def test_invalid_envelope_rejected_without_touching_store(bad):
    worker, ctx, service = setup_worker()
    before = ctx.store.snapshot()
    with pytest.raises(DomainError) as error:
        process_incoming(service, ctx, bad)
    assert error.value.status == 422
    assert ctx.store.snapshot() == before
    assert service.calls == 0


def test_retry_goes_only_to_own_queue_and_acks_after_confirm(monkeypatch):
    monkeypatch.setattr("common.messaging.time.sleep", lambda _: None)
    worker, ctx, service = setup_worker(CountingService(failure=DomainError(503, "temporarily unavailable")))
    channel = FakeChannel()
    raw = body()
    worker.callback(channel, delivery(), pika.BasicProperties(headers={}), raw)
    assert [op[0] for op in channel.operations] == ["publish", "ack"]
    retry = channel.published[0]
    assert retry["exchange"] == ""
    assert retry["routing_key"] == "billiards.bookings"
    assert retry["mandatory"] is True
    assert retry["body"] == raw
    assert retry["properties"].delivery_mode == 2
    assert retry["properties"].headers == {"x-attempt": 2, "x-original-key": "payment.succeeded"}
    assert ctx.store.snapshot()["outbox"] == []
    assert ctx.store.snapshot()["inbox"] == {}


def test_failed_retry_publication_leaves_original_unacknowledged(monkeypatch):
    monkeypatch.setattr("common.messaging.time.sleep", lambda _: None)
    worker, ctx, service = setup_worker(CountingService(failure=DomainError(503, "temporarily unavailable")))
    channel = FakeChannel(fail_publish_at=1)
    with pytest.raises(pika.exceptions.NackError):
        worker.callback(channel, delivery(), pika.BasicProperties(headers={}), body())
    assert channel.acknowledged == []
    assert channel.rejected == []
    assert channel.published == []
    assert ctx.store.snapshot()["inbox"] == {}


def test_retry_restores_event_routing_key_and_processes_once():
    worker, ctx, service = setup_worker()
    event = envelope()
    channel = FakeChannel()
    worker.receive(channel, delivery(key="billiards.bookings", exchange=""),
                   pika.BasicProperties(headers={"x-attempt": 2, "x-original-key": event["type"]}), body(event))
    assert service.calls == 1
    assert channel.acknowledged == [{"delivery_tag": 7}]
    assert channel.published == []
    assert event["id"] in ctx.store.snapshot()["inbox"]


@pytest.mark.parametrize("raw,headers", [
    (b"not-json", {}), (b"null", {}), (b"[]", {}),
    (body(envelope(id=4)), {}), (body(), {"x-attempt": "two"}),
    (body(), {"x-attempt": 0}), (body(), {"x-attempt": True}),
    (body(), ["invalid", "headers"]),
    (body(envelope(type="payment.failed")), {}),
])
def test_poison_messages_are_confirmed_to_dlq_without_requeue_loops(raw, headers):
    worker, ctx, service = setup_worker()
    channel = FakeChannel()
    worker.receive(channel, delivery(), pika.BasicProperties(headers=headers), raw)
    assert service.calls == 0
    assert [op[0] for op in channel.operations] == ["publish", "ack"]
    dead = channel.published[0]
    assert (dead["exchange"], dead["routing_key"], dead["body"]) == (DLX, "bookings", raw)
    assert dead["mandatory"] is True
    assert dead["properties"].delivery_mode == 2
    assert dead["properties"].headers["x-original-key"] == "payment.succeeded"
    assert dead["properties"].headers["x-error"]
    assert ctx.store.snapshot()["inbox"] == {}


def test_third_temporary_failure_goes_to_dlq_instead_of_fourth_retry():
    worker, ctx, service = setup_worker(CountingService(failure=DomainError(503, "still unavailable")))
    channel = FakeChannel()
    worker.callback(channel, delivery(), pika.BasicProperties(headers={"x-attempt": 3}), body())
    assert channel.published[0]["exchange"] == DLX
    assert channel.published[0]["properties"].headers["x-attempt"] == 3
    assert channel.acknowledged == [{"delivery_tag": 7}]
    assert ctx.store.snapshot()["outbox"] == []


def test_failed_dlq_publication_leaves_poison_message_unacknowledged():
    worker, ctx, service = setup_worker()
    channel = FakeChannel(fail_publish_at=1)
    with pytest.raises(pika.exceptions.NackError):
        worker.callback(channel, delivery(), pika.BasicProperties(headers={}), b"[]")
    assert channel.acknowledged == []
    assert channel.rejected == []
    assert channel.published == []


def test_lost_ack_after_commit_redelivery_does_not_repeat_business_effect():
    worker, ctx, service = setup_worker()
    event = envelope()
    with pytest.raises(pika.exceptions.ConnectionClosedByBroker):
        worker.callback(FakeChannel(fail_ack=True), delivery(), pika.BasicProperties(), body(event))
    retry_channel = FakeChannel()
    worker.callback(retry_channel, delivery(tag=8), pika.BasicProperties(), body(event))
    assert service.calls == 1
    assert retry_channel.acknowledged == [{"delivery_tag": 8}]
    assert len(ctx.store.snapshot()["outbox"]) == 1


def test_outbox_removes_only_confirmed_messages_and_retains_failed_tail():
    worker, ctx, service = setup_worker()
    with ctx.store.transaction() as tx:
        first = deepcopy(tx.emit("booking.confirmed", {"booking_id": "one"}))
        second = deepcopy(tx.emit("booking.confirmed", {"booking_id": "two"}))
        third = deepcopy(tx.emit("booking.cancelled", {"booking_id": "three"}))
    channel = FakeChannel(fail_publish_at=2)
    with pytest.raises(pika.exceptions.NackError):
        worker.flush(channel)
    assert [e["id"] for e in ctx.store.snapshot()["outbox"]] == [second["id"], third["id"]]
    assert json.loads(channel.published[0]["body"])["id"] == first["id"]
    recovered = FakeChannel()
    worker.flush(recovered)
    assert [json.loads(p["body"])["id"] for p in recovered.published] == [second["id"], third["id"]]
    assert all(p["mandatory"] and p["properties"].delivery_mode == 2 for p in recovered.published)
    assert ctx.store.snapshot()["outbox"] == []
