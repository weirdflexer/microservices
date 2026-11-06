"""Независимая от брокера конфигурация и правила четырёх вариантов."""

from __future__ import annotations

import json
import math
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PROFILES = Path(__file__).with_name("profiles.json")


@dataclass(frozen=True)
class Profile:
    student: str
    name: str
    group: str
    surname: str
    last_digit: int
    variant: int

    @property
    def queue(self) -> str:
        return f"{self.group}_{self.surname}"

    @property
    def exchange(self) -> str:
        return f"practice5.{self.queue}"

    @property
    def exchange_type(self) -> str:
        return {1: "topic", 2: "direct", 3: "fanout", 4: "direct"}[self.variant]

    @property
    def durable(self) -> bool:
        return self.variant in (2, 4)

    @property
    def auto_delete(self) -> bool:
        return not self.durable

    @property
    def binding(self) -> str:
        return {1: "billiards.*", 2: "billiards.demo", 3: "", 4: "billiards.demo"}[self.variant]

    @property
    def routing_key(self) -> str:
        return "" if self.variant == 3 else "billiards.demo"


def load_profiles(path: str | Path = PROFILES) -> dict[str, Profile]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    group = raw["group"]
    if not isinstance(group, str) or not re.fullmatch(r"[a-z0-9-]+", group):
        raise ValueError("group должен содержать латинские буквы, цифры или дефис")
    profiles: dict[str, Profile] = {}
    used_variants: set[int] = set()
    used_queues: set[str] = set()
    for key, item in raw["students"].items():
        variant = item["variant"]
        last_digit = item["last_digit"]
        surname = item["surname"]
        if type(variant) is not int or variant not in (1, 2, 3, 4):
            raise ValueError(f"{key}: variant должен быть целым числом от 1 до 4")
        if variant in used_variants:
            raise ValueError(f"Вариант {variant} назначен более одному студенту")
        if type(last_digit) is not int or not 0 <= last_digit <= 9:
            raise ValueError(f"{key}: last_digit должен быть цифрой от 0 до 9")
        if not isinstance(surname, str) or not re.fullmatch(r"[a-z0-9-]+", surname):
            raise ValueError(f"{key}: surname должен быть записан латиницей")
        profile = Profile(key, item["name"], group, surname, last_digit, variant)
        if profile.queue in used_queues:
            raise ValueError(f"Повтор имени очереди {profile.queue}")
        profiles[key] = profile
        used_variants.add(variant)
        used_queues.add(profile.queue)
    if not profiles:
        raise ValueError("В profiles.json нет студентов")
    return profiles


def delay_seconds(
    variant: int, text: str, randint: Callable[[int, int], int] = random.randint
) -> float:
    """Задержка по методичке. Длина — число Unicode-символов, не UTF-8 байтов."""
    if variant == 1:
        return float(text.count("."))
    if variant == 2:
        return float(len(text))
    if variant == 3:
        return float(randint(1, 5))
    if variant == 4:
        try:
            delay = float(text.strip())
        except ValueError as exc:
            raise ValueError("Тело варианта 4 должно содержать число секунд") from exc
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("Задержка должна быть конечным неотрицательным числом")
        return delay
    raise ValueError("Неизвестный вариант: допустимы 1, 2, 3, 4")
