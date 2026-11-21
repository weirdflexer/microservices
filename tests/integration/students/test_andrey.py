from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def update_table(stack, table_id, **changes):
    return stack.request("catalog", "PATCH", f"/api/tables/{table_id}", body=changes)


def set_hourly_rate(stack, game_type, rate):
    stack.request(
        "pricing",
        "POST",
        "/api/tariffs",
        status=201,
        body={"game_type": game_type, "hourly_rate": rate},
    )


def request_quote(stack, slot, expected_status=201):
    return stack.request(
        "pricing",
        "POST",
        "/api/quotes",
        status=expected_status,
        body={"table_id": slot["table"]["id"], **slot["interval"]},
    )


def test_publication_persisted(live_stack, db_bucket):
    """Публикация обновляет ту же карточку стола в PostgreSQL."""
    draft = {
        "number": uuid4().int % 2_000_000_000 + 1,
        "game_type": "pool",
        "published": False,
    }
    table = live_stack.request("catalog", "POST", "/api/tables", status=201, body=draft)

    update_table(live_stack, table["id"], published=True)

    saved = db_bucket("catalog", "tables")[table["id"]]
    assert saved["published"] is True
    assert saved["number"] == draft["number"]
    assert saved["game_type"] == draft["game_type"]


def test_quote_preserves_old_rate_in_postgres(live_stack, one_hour_slot, db_bucket):
    """После смены тарифа в базе остаются предложения по обеим ценам."""
    original = one_hour_slot["quote"]
    set_hourly_rate(live_stack, "pool", 70_000)

    latest = request_quote(live_stack, one_hour_slot)

    quotes = db_bucket("pricing", "quotes")
    assert quotes[original["id"]]["amount"] == 60_000
    assert quotes[latest["id"]]["amount"] == 70_000


def test_unpublished_table_rejected_by_pricing(live_stack, one_hour_slot):
    """Сервис тарифов отклоняет расчёт для снятого с публикации стола."""
    update_table(live_stack, one_hour_slot["table"]["id"], published=False)

    request_quote(live_stack, one_hour_slot, expected_status=409)


def test_changed_game_selects_new_tariff(live_stack, one_hour_slot):
    """После смены вида игры применяется тариф снукера."""
    set_hourly_rate(live_stack, "snooker", 89_000)
    update_table(live_stack, one_hour_slot["table"]["id"], game_type="snooker")

    quote = request_quote(live_stack, one_hour_slot)

    assert one_hour_slot["quote"]["amount"] == 60_000
    assert quote["amount"] == 89_000
