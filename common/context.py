from dataclasses import dataclass
from typing import Any, Callable

from common.time import utcnow


@dataclass
class Context:
    name: str
    store: Any
    http: Any
    now: Callable = utcnow
    settings: Any = None
