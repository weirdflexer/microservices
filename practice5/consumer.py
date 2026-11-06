"""Запуск: python -m practice5.consumer --student danek."""

from __future__ import annotations

import logging

from practice5.broker import connect, declare_topology, make_callback, parser, resolve_profile


def main(argv: list[str] | None = None) -> int:
    arg_parser = parser("Практика 5: получатель RabbitMQ с симуляцией нагрузки")
    args = arg_parser.parse_args(argv)
    profile = resolve_profile(args, arg_parser)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    import pika

    connection = None
    channel = None
    try:
        connection = connect()
        channel = connection.channel()
        declare_topology(channel, profile, args.mode, args.binding)
        channel.basic_qos(prefetch_count=1)
        channel.basic_consume(
            queue=profile.queue,
            on_message_callback=make_callback(profile, connection.sleep),
            auto_ack=False,
        )
        logging.info("WAITING student=%s variant=%s queue=%s mode=%s", args.student, profile.variant, profile.queue, args.mode)
        channel.start_consuming()
        return 0
    except KeyboardInterrupt:
        logging.info("Остановка consumer; неподтверждённая доставка будет обработана брокером")
        return 0
    except pika.exceptions.AMQPError as exc:
        logging.error("RabbitMQ: %s", type(exc).__name__)
        return 2
    finally:
        if connection is not None and connection.is_open:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
