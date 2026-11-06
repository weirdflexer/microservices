"""Unit of Work: состояние, inbox и outbox фиксируются одной транзакцией.

Учебное упрощение: одна JSON-запись состояния на сервис. Блокировка этой
записи сериализует команды, включая проверку пересечений в расписании.
Для большого объёма нужны отдельные таблицы агрегатов и более узкие блокировки.
"""
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from uuid import uuid4

from sqlalchemy import JSON, Integer, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from common.time import iso, utcnow


def initial_state():
    return {"data": {}, "inbox": {}, "outbox": []}


class Transaction:
    def __init__(self, state, name):
        self.state = state
        self.name = name
        self.data = state["data"]
        self.inbox = state["inbox"]
        self.outbox = state["outbox"]

    def bucket(self, name):
        return self.data.setdefault(name, {})

    def emit(self, event_type, data, correlation_id=None):
        event = {
            "id": str(uuid4()), "type": event_type,
            "occurred_at": iso(utcnow()), "producer": self.name,
            "correlation_id": correlation_id or data.get("booking_id"),
            "data": deepcopy(data),
        }
        self.outbox.append(event)
        return event


class MemoryStore:
    def __init__(self, name):
        self.name = name
        self._state = initial_state()
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            state = deepcopy(self._state)
            yield Transaction(state, self.name)
            # Detach returned business objects from the committed state.
            self._state = deepcopy(state)

    def snapshot(self):
        with self._lock:
            return deepcopy(self._state)

    def close(self):
        pass


class Base(DeclarativeBase):
    pass


class ServiceState(Base):
    __tablename__ = "service_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document: Mapped[dict] = mapped_column(JSON, nullable=False)


class SQLStore:
    def __init__(self, name, url):
        self.name = name
        self._lock = RLock()
        self.engine = create_engine(url, pool_pre_ping=True)
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
                conn.execute(insert(ServiceState).values(id=1, document=initial_state()).on_conflict_do_nothing())
            elif not conn.execute(select(ServiceState.id)).first():
                conn.execute(ServiceState.__table__.insert().values(id=1, document=initial_state()))

    @contextmanager
    def transaction(self):
        with self._lock, Session(self.engine) as session, session.begin():
            row = session.execute(select(ServiceState).where(ServiceState.id == 1).with_for_update()).scalar_one()
            state = deepcopy(row.document)
            yield Transaction(state, self.name)
            row.document = deepcopy(state)

    def snapshot(self):
        with self.transaction() as tx:
            return deepcopy(tx.state)

    def close(self):
        self.engine.dispose()
