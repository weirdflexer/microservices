"""RabbitMQ adapter: durable topic, outbox, inbox, manual ack, bounded retry, DLQ."""
import json
import logging
import time
from copy import deepcopy
from threading import Event, Thread
from uuid import UUID

import pika

from common.errors import DomainError
from common.time import iso, parse_time, utcnow

log = logging.getLogger(__name__)
EXCHANGE = "billiards.events"
DLX = "billiards.dead"
BINDINGS = {
    "users": [], "catalog": [], "pricing": [],
    "schedule": ["booking.cancelled", "booking.expired"],
    "bookings": ["payment.created", "payment.succeeded", "payment.failed", "payment.refunded", "payment.refund_failed", "schedule.hold_expired", "session.completed", "session.no_show"],
    "payments": ["booking.payment_requested", "booking.refund_requested"],
    "sessions": ["booking.confirmed", "booking.cancelled", "booking.expired"],
    "notifications": ["booking.confirmed", "booking.cancelled", "booking.expired", "payment.refunded"],
}


def declare_topology(channel):
    channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
    channel.exchange_declare(exchange=DLX, exchange_type="direct", durable=True)
    # Inspector queue also receives internal domain facts without business subscribers.
    channel.queue_declare(queue="billiards.audit", durable=True, arguments={"x-max-length": 10000})
    channel.queue_bind(queue="billiards.audit", exchange=EXCHANGE, routing_key="#")
    for service, keys in BINDINGS.items():
        if not keys:
            continue
        queue = f"billiards.{service}"
        channel.queue_declare(queue=queue + ".dead", durable=True)
        channel.queue_bind(queue=queue + ".dead", exchange=DLX, routing_key=service)
        channel.queue_declare(queue=queue, durable=True, arguments={"x-dead-letter-exchange": DLX, "x-dead-letter-routing-key": service})
        for key in keys:
            channel.queue_bind(queue=queue, exchange=EXCHANGE, routing_key=key)


def validate_envelope(event):
    """Reject malformed JSON before business code and normalize the inbox key."""
    try:
        if not isinstance(event, dict):
            raise ValueError("Envelope must be an object")
        if not isinstance(event["id"], str):
            raise ValueError("Event id must be a UUID string")
        event_id = str(UUID(event["id"]))
        if not isinstance(event["occurred_at"], str):
            raise ValueError("Event time must be a string")
        parse_time(event["occurred_at"])
        if not isinstance(event["data"], dict):
            raise ValueError("Event data must be an object")
        if not isinstance(event["type"], str) or not event["type"].strip():
            raise ValueError("Event type is required")
        if not isinstance(event["producer"], str) or not event["producer"].strip():
            raise ValueError("Event producer is required")
        if event["correlation_id"] is not None and not isinstance(event["correlation_id"], str):
            raise ValueError("Correlation id must be a string or null")
        return event_id
    except (ValueError, KeyError, TypeError, DomainError) as exc:
        raise DomainError(422, "Некорректный конверт события") from exc


def process_incoming(service, ctx, event):
    event_id = validate_envelope(event)
    with ctx.store.transaction() as tx:
        if event_id in tx.inbox:
            return False
        service.handle(event, tx)
        tx.inbox[event_id] = iso(utcnow())
    return True


class BrokerWorker:
    def __init__(self, ctx, service):
        self.ctx = ctx
        self.service = service
        self.stop_event = Event()
        self.connected = False
        self.thread = Thread(target=self.run, name=f"amqp-{ctx.name}", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=15)

    def callback(self, ch, method, properties, body):
        attempt = 1
        headers = {}
        try:
            raw_headers = properties.headers or {}
            if not isinstance(raw_headers, dict):
                raise DomainError(422, "Некорректные заголовки сообщения")
            headers = raw_headers
            attempt = headers.get("x-attempt", 1)
            if type(attempt) is not int or attempt < 1:
                raise DomainError(422, "Некорректный номер попытки доставки")
            event = json.loads(body)
            validate_envelope(event)
            if event["type"] != method.routing_key:
                raise DomainError(422, "Routing key не совпадает с типом события")
            process_incoming(self.service, self.ctx, event)
        except Exception as exc:
            permanent = isinstance(exc, (ValueError, KeyError, TypeError)) or isinstance(exc, DomainError) and exc.status < 500
            log.exception("Event handling failed service=%s attempt=%s", self.ctx.name, attempt)
            if permanent or attempt >= 3:
                # Confirm the DLQ publication before ack. Relying only on a nack
                # and classic-queue dead-lettering could lose a failed publish.
                dead_headers = {"x-original-key": method.routing_key,
                                "x-error": str(exc)[:500]}
                if type(attempt) is int and attempt >= 1:
                    dead_headers["x-attempt"] = attempt
                ch.basic_publish(exchange=DLX, routing_key=self.ctx.name, body=body,
                                 properties=pika.BasicProperties(delivery_mode=2, content_type="application/json", headers=dead_headers), mandatory=True)
                ch.basic_ack(delivery_tag=method.delivery_tag)
            else:
                # A failed confirm propagates to run(), closing the connection.
                # The original remains unacked and RabbitMQ redelivers it.
                time.sleep(0.2 * attempt)
                retry_headers = {**headers, "x-attempt": attempt + 1,
                                 "x-original-key": event["type"]}
                ch.basic_publish(exchange="", routing_key=f"billiards.{self.ctx.name}", body=body,
                                 properties=pika.BasicProperties(delivery_mode=2, content_type="application/json", headers=retry_headers), mandatory=True)
                ch.basic_ack(delivery_tag=method.delivery_tag)
            return
        ch.basic_ack(delivery_tag=method.delivery_tag)

    def receive(self, ch, method, properties, body):
        # A retried message arrived through the default exchange; restore its event key.
        if method.exchange == "" and isinstance(properties.headers, dict) and properties.headers.get("x-original-key"):
            method.routing_key = properties.headers["x-original-key"]
        self.callback(ch, method, properties, body)

    def flush(self, channel):
        with self.ctx.store.transaction() as tx:
            events = deepcopy(tx.outbox[:100])
        for event in events:
            channel.basic_publish(exchange=EXCHANGE, routing_key=event["type"],
                                  body=json.dumps(event, ensure_ascii=False).encode(),
                                  properties=pika.BasicProperties(delivery_mode=2, content_type="application/json", message_id=event["id"]), mandatory=True)
            with self.ctx.store.transaction() as tx:
                tx.outbox[:] = [e for e in tx.outbox if e["id"] != event["id"]]

    def run(self):
        while not self.stop_event.is_set():
            connection = None
            try:
                params = pika.URLParameters(self.ctx.settings.rabbitmq_url)
                params.socket_timeout = 5
                params.stack_timeout = 10
                params.blocked_connection_timeout = 5
                params.heartbeat = 60
                connection = pika.BlockingConnection(params)
                channel = connection.channel()
                declare_topology(channel)
                channel.confirm_delivery()
                channel.basic_qos(prefetch_count=1)
                if BINDINGS[self.ctx.name]:
                    channel.basic_consume(queue=f"billiards.{self.ctx.name}", on_message_callback=self.receive, auto_ack=False)
                self.connected = True
                while not self.stop_event.is_set():
                    self.flush(channel)
                    connection.process_data_events(time_limit=0.2)
                    self.stop_event.wait(0.05)
            except Exception:
                log.exception("RabbitMQ connection lost; retrying")
            finally:
                self.connected = False
                if connection and connection.is_open:
                    try:
                        connection.close()
                    except Exception:
                        log.exception("RabbitMQ close failed")
            self.stop_event.wait(2)
