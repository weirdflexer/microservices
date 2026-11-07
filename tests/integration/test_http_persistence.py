"""Two-phase persistence check; the caller restarts the actual containers between phases.

HTTP_PERSISTENCE_MODE=write creates a confirmed booking and records public responses.
HTTP_PERSISTENCE_MODE=verify reads them again, without creating replacement records.
No service code, SQL connection, Docker or Podman API is imported here.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def read_record(stack, spec):
    if "selector_id" not in spec:
        return stack.request(spec["service"], "GET", spec["path"], params=spec.get("params"))
    offset = 0
    while True:
        rows = stack.request(spec["service"], "GET", spec["path"],
                             params={**spec.get("params", {}), "offset": offset, "limit": 100})
        for row in rows:
            if row["id"] == spec["selector_id"]:
                return row
        assert rows, f"Persisted {spec['service']} record {spec['selector_id']} is missing"
        offset += len(rows)


def test_public_state_survives_container_restart(request):
    mode = os.environ.get("HTTP_PERSISTENCE_MODE")
    if mode not in ("write", "verify"):
        pytest.skip("Set HTTP_PERSISTENCE_MODE=write or verify and run around a container restart")
    stack = request.getfixturevalue("live_stack")
    path = Path(os.environ.get("HTTP_PERSISTENCE_FILE", "/private/tmp/micro-polyglot-persistence.json"))
    if mode == "write":
        prepared = stack.prepare(hours_ahead=6)
        booking = stack.pay(stack.book(prepared))
        visits = stack.wait(lambda: stack.visits(booking["id"]), lambda rows: len(rows) == 1)
        notices = stack.wait(lambda: stack.notices(prepared["user"]["id"]),
                             lambda rows: any(row["kind"] == "confirmation" for row in rows))
        confirmation = next(row for row in notices if row["kind"] == "confirmation")
        stack.request("notifications", "POST", f"/api/notifications/{confirmation['id']}:send")
        specs = [
            {"service": "users", "path": f"/api/users/{prepared['user']['id']}"},
            {"service": "catalog", "path": f"/api/tables/{prepared['table']['id']}"},
            {"service": "pricing", "path": "/api/tariffs", "selector_id": prepared["tariff"]["id"]},
            {"service": "schedule", "path": f"/api/tables/{prepared['table']['id']}/availability",
             "params": prepared["interval"]},
            {"service": "bookings", "path": f"/api/bookings/{booking['id']}"},
            {"service": "payments", "path": f"/api/payments/{booking['payment_id']}"},
            {"service": "sessions", "path": f"/api/sessions/{visits[0]['id']}"},
            {"service": "notifications", "path": f"/api/notifications/{confirmation['id']}"},
        ]
        records = [{**spec, "expected": read_record(stack, spec)} for spec in specs]
        snapshot = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                    "stack_urls": stack.urls, "booking_id": booking["id"], "records": records}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        assert len(records) == 8
        print(f"Persistence snapshot written: {path}; restart containers, then run verify")
    else:
        assert path.is_file(), f"Snapshot not found: run HTTP_PERSISTENCE_MODE=write first ({path})"
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        assert snapshot["schema_version"] == 1
        assert len(snapshot["records"]) == 8
        assert snapshot["stack_urls"] == stack.urls, "Verification must target the same service addresses"
        for spec in snapshot["records"]:
            actual = read_record(stack, spec)
            assert actual == spec["expected"], (
                f"Persistent public state changed or disappeared in {spec['service']}: "
                f"expected {spec['expected']!r}; actual {actual!r}"
            )
        print(f"Verified all eight persisted service records from {path}")
