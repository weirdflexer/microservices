"""Общие операции с RabbitMQ; объявления одинаковы у producer и consumer."""

from __future__ import annotations

import argparse
import logging
import os
from typing import Callable

from practice5.config import PROFILES, Profile, delay_seconds, load_profiles

LOGGER = logging.getLogger(__name__)
DEFAULT_URL = "amqp://micro:micro@localhost:5672/"


def parser(description: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=description)
    result.add_argument("--student", required=True, help="danek, sasha, andrey или anvar")
    result.add_argument("--profiles", default=str(PROFILES), help="JSON с распределением вариантов")
    result.add_argument("--mode", choices=("simple", "variant"), default="variant")
    result.add_argument("--binding", help="Ключ привязки: по умолчанию определяется вариантом")
    return result


def resolve_profile(args: argparse.Namespace, arg_parser: argparse.ArgumentParser) -> Profile:
    try:
        profiles = load_profiles(args.profiles)
        return profiles[args.student]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        arg_parser.error(f"Ошибка профиля: {exc}")
        raise AssertionError("argparse.error завершается SystemExit")


def connect():
    # Импорт отложен: правила вариантов можно тестировать без живого RabbitMQ.
    import pika

    parameters = pika.URLParameters(os.environ.get("RABBITMQ_URL", DEFAULT_URL))
    parameters.blocked_connection_timeout = 15
    parameters.socket_timeout = 10
    parameters.connection_attempts = 3
    parameters.retry_delay = 2
    return pika.BlockingConnection(parameters)


def declare_topology(channel, profile: Profile, mode: str, binding: str | None = None) -> str:
    if mode not in ("simple", "variant"):
        raise ValueError("mode должен быть simple или variant")
    channel.queue_declare(
        queue=profile.queue,
        durable=profile.durable,
        auto_delete=profile.auto_delete,
        exclusive=False,
    )
    if mode == "simple":
        # Встроенный обменник автоматически маршрутизирует по имени очереди.
        return ""
    channel.exchange_declare(
        exchange=profile.exchange,
        exchange_type=profile.exchange_type,
        durable=profile.durable,
        auto_delete=False,
    )
    channel.queue_bind(
        queue=profile.queue,
        exchange=profile.exchange,
        routing_key=profile.binding if binding is None else binding,
    )
    return profile.exchange


def make_callback(profile: Profile, sleep: Callable[[float], None]):
    def callback(channel, method, properties, body: bytes):
        try:
            message = body.decode("utf-8")
            delay = delay_seconds(profile.variant, message)
        except (UnicodeDecodeError, ValueError) as exc:
            # Ядовитое сообщение не должно бесконечно возвращаться в очередь.
            LOGGER.error("REJECT queue=%s: %s", profile.queue, exc)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        LOGGER.info("RECEIVED queue=%s delay=%ss body=%r", profile.queue, delay, message)
        # connection.sleep обслуживает heartbeat, в отличие от обычного time.sleep.
        # При аварии до ack RabbitMQ повторно доставит неподтверждённое сообщение.
        sleep(delay)
        channel.basic_ack(delivery_tag=method.delivery_tag)
        LOGGER.info("ACK queue=%s", profile.queue)

    return callback
