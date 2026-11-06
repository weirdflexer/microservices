"""Проверка четырёх индивидуальных вариантов с настоящим RabbitMQ."""
import logging
import time

from practice5.broker import connect, declare_topology, make_callback
from practice5.config import load_profiles
from practice5.producer import main as publish


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("pika").setLevel(logging.CRITICAL)
    for student, profile in load_profiles().items():
        for mode in ("simple", "variant"):
            connection = connect()
            try:
                channel = connection.channel()
                declare_topology(channel, profile, mode)
                channel.basic_qos(prefetch_count=1)
                received = []
                real_callback = make_callback(profile, connection.sleep)

                def callback(ch, method, properties, body):
                    real_callback(ch, method, properties, body)
                    received.append(body)

                channel.basic_consume(profile.queue, callback, auto_ack=False)
                body = "1" if profile.variant == 4 else "."
                assert publish(["--student", student, "--mode", mode, body]) == 0
                deadline = time.monotonic() + 20
                while not received and time.monotonic() < deadline:
                    connection.process_data_events(time_limit=0.2)
                assert received == [body.encode()], (student, mode, received)
                print(f"PASS {student} variant={profile.variant} mode={mode} queue={profile.queue}")
            finally:
                if connection.is_open:
                    connection.close()


if __name__ == "__main__":
    main()
