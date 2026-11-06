from datetime import datetime, timezone

from common.errors import DomainError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | datetime) -> datetime:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("Timezone required")
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise DomainError(422, "Укажите время ISO 8601 с часовым поясом") from exc


def iso(value: datetime) -> str:
    return parse_time(value).isoformat()


def validate_interval(start, end, now=None):
    start, end = parse_time(start), parse_time(end)
    duration = (end - start).total_seconds()
    if start.minute % 30 or start.second or start.microsecond:
        raise DomainError(422, "Начало игры должно быть кратно 30 минутам")
    if not 3600 <= duration <= 14400 or duration % 1800:
        raise DomainError(422, "Длительность: от 1 до 4 часов с шагом 30 минут")
    if now is not None and start <= parse_time(now):
        raise DomainError(422, "Начало игры должно быть в будущем")
    return start, end
