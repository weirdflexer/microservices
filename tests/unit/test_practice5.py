"""Проверки требований индивидуальных вариантов; брокер не нужен."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from practice5.broker import declare_topology, make_callback
from practice5.config import PROFILES, delay_seconds, load_profiles


@pytest.mark.parametrize(
    ("variant", "body", "expected"),
    [
        (1, "Бронь.. подтверждена.", 3),
        (1, "без точек", 0),
        (1, "", 0),
        (2, "Привет", 6),  # 6 символов, хотя UTF-8 содержит 12 байтов.
        (2, "", 0),
        (4, "2", 2),
        (4, " 1.5 ", 1.5),
        (4, "0", 0),
    ],
)
def test_delay_matches_manual(variant, body, expected):
    assert delay_seconds(variant, body) == expected


def test_random_delay_uses_inclusive_bounds_one_to_five():
    random = Mock(return_value=5)
    assert delay_seconds(3, "тело не влияет", randint=random) == 5
    random.assert_called_once_with(1, 5)


@pytest.mark.parametrize("body", ["", "abc", "-1", "NaN", "inf", "-inf", "1e999"])
def test_invalid_numeric_delay_is_rejected(body):
    with pytest.raises(ValueError):
        delay_seconds(4, body)


def test_profiles_have_four_distinct_variants_and_expected_names():
    profiles = load_profiles()
    assert {name: p.variant for name, p in profiles.items()} == {
        "danek": 1, "sasha": 2, "andrey": 3, "anvar": 4
    }
    assert profiles["andrey"].last_digit == 8
    assert profiles["andrey"].queue == "ikbo-63-23_isachenko"
    assert profiles["sasha"].queue == "ikbo-63-23_volentir"
    assert profiles["anvar"].queue == "ikbo-63-23_nartadzhiev"


def test_duplicate_assigned_variant_is_invalid(tmp_path):
    data = json.loads(PROFILES.read_text(encoding="utf-8"))
    data["students"]["andrey"]["variant"] = 4
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="более одному"):
        load_profiles(path)


@pytest.mark.parametrize(
    ("student", "exchange_type", "durable", "auto_delete", "binding"),
    [
        ("danek", "topic", False, True, "billiards.*"),
        ("sasha", "direct", True, False, "billiards.demo"),
        ("andrey", "fanout", False, True, ""),
        ("anvar", "direct", True, False, "billiards.demo"),
    ],
)
def test_variant_topologies_match_assignment(student, exchange_type, durable, auto_delete, binding):
    profile = load_profiles()[student]
    channel = Mock()
    exchange = declare_topology(channel, profile, "variant")
    channel.queue_declare.assert_called_once_with(
        queue=profile.queue, durable=durable, auto_delete=auto_delete, exclusive=False
    )
    assert exchange == profile.exchange
    assert channel.exchange_declare.call_args.kwargs["exchange_type"] == exchange_type
    assert channel.exchange_declare.call_args.kwargs["durable"] == durable
    assert channel.queue_bind.call_args.kwargs["routing_key"] == binding


def test_simple_mode_uses_builtin_exchange_without_explicit_binding():
    profile = load_profiles()["sasha"]
    channel = Mock()
    assert declare_topology(channel, profile, "simple") == ""
    channel.queue_declare.assert_called_once()
    channel.exchange_declare.assert_not_called()
    channel.queue_bind.assert_not_called()


def test_callback_acknowledges_only_after_work_finishes():
    channel = Mock()
    observed = []

    def sleep(seconds):
        channel.basic_ack.assert_not_called()
        observed.append(seconds)

    callback = make_callback(load_profiles()["danek"], sleep)
    callback(channel, SimpleNamespace(delivery_tag=42), None, b"... ready")
    assert observed == [3]
    channel.basic_ack.assert_called_once_with(delivery_tag=42)
    channel.basic_nack.assert_not_called()


@pytest.mark.parametrize("body", [b"NaN", b"-2", b"not a number", b"\xff"])
def test_callback_discards_invalid_message_without_infinite_requeue(body):
    channel = Mock()
    sleep = Mock()
    callback = make_callback(load_profiles()["anvar"], sleep)
    callback(channel, SimpleNamespace(delivery_tag=9), None, body)
    sleep.assert_not_called()
    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_called_once_with(delivery_tag=9, requeue=False)


def test_interrupted_work_does_not_acknowledge_message():
    channel = Mock()
    sleep = Mock(side_effect=KeyboardInterrupt)
    callback = make_callback(load_profiles()["sasha"], sleep)
    with pytest.raises(KeyboardInterrupt):
        callback(channel, SimpleNamespace(delivery_tag=7), None, b"job")
    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_not_called()
