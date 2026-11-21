"""Read service state via psql inside the project's PostgreSQL container."""

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def db_bucket():
    def read(service, bucket):
        command = shlex.split(os.environ.get("TEST_COMPOSE", "docker compose"))
        sql = "SELECT document->'data'->:'bucket' FROM service_state WHERE id=1;"
        result = subprocess.run(
            [
                *command,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-XAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"bucket={bucket}",
                "-U",
                service,
                "-d",
                service,
            ],
            cwd=Path(__file__).resolve().parents[3],
            input=sql,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        return json.loads(result.stdout.strip() or "{}")

    return read


@pytest.fixture
def one_hour_slot(live_stack):
    """Опубликованный стол, час игры и предложение на 60 000 копеек."""
    return live_stack.prepare(duration_hours=1)


@pytest.fixture
def unpaid_booking(live_stack, one_hour_slot):
    """Бронь с удержанным интервалом и созданным платежом."""
    return live_stack.book(one_hour_slot)


@pytest.fixture
def confirmed_booking(live_stack, unpaid_booking):
    return live_stack.pay(unpaid_booking)


@pytest.fixture
def cancel_booking(live_stack):
    def cancel(booking):
        path = f"/api/bookings/{booking['id']}:cancel"
        return live_stack.request("bookings", "POST", path)

    return cancel
