"""Запуск: python -m practice5.producer --student danek 'Один..два.'"""

from __future__ import annotations

from uuid import uuid4

from practice5.broker import connect, declare_topology, parser, resolve_profile


def main(argv: list[str] | None = None) -> int:
    arg_parser = parser("Практика 5: отправитель RabbitMQ")
    arg_parser.add_argument("message", help="Тело сообщения в UTF-8")
    arg_parser.add_argument("--routing-key", help="Ключ публикации; simple использует имя очереди")
    args = arg_parser.parse_args(argv)
    profile = resolve_profile(args, arg_parser)
    if args.mode == "simple" and args.routing_key is not None:
        arg_parser.error("В simple ключ маршрутизации уже равен имени очереди")

    import pika

    connection = None
    try:
        connection = connect()
        channel = connection.channel()
        exchange = declare_topology(channel, profile, args.mode, args.binding)
        routing_key = profile.queue if args.mode == "simple" else (
            profile.routing_key if args.routing_key is None else args.routing_key
        )
        channel.confirm_delivery()
        channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=args.message.encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="text/plain",
                content_encoding="utf-8",
                delivery_mode=2 if profile.durable else 1,
                message_id=str(uuid4()),
            ),
            mandatory=True,
        )
        print(f"CONFIRMED queue={profile.queue} exchange={exchange or '(default)'} body={args.message!r}")
        return 0
    except pika.exceptions.UnroutableError:
        print("ERROR: exchange не направил сообщение ни в одну очередь; проверьте binding/routing-key")
        return 2
    except pika.exceptions.AMQPError as exc:
        print(f"ERROR: RabbitMQ не подтвердил отправку ({type(exc).__name__})")
        return 2
    finally:
        if connection is not None and connection.is_open:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
