"""saga 存在哪裡：狀態、saga_log、要送的命令，一次存（同一個交易）。

change(saga_id, apply)：鎖住這個 saga → apply 改它 → 存回去。apply 回 None＝什麼都不存。
"""
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime
from typing import Protocol

from foodgo import contracts
from foodgo.saga.machine import Entry, OrderSaga

Apply = Callable[[OrderSaga | None], OrderSaga | None]


class SagaRepository(Protocol):
    def change(self, saga_id: str, apply: Apply, *, message_id: str | None = None) -> OrderSaga | None:
        """一個交易：鎖住 saga、apply、存狀態＋saga_log＋要送的命令。message_id 處理過了就什麼都不做，回 None。"""
        ...

    def get(self, saga_id: str) -> OrderSaga | None: ...

    def due(self, now: datetime) -> list[str]:
        """等回覆等到時間到了的 saga。"""
        ...

    def log(self, saga_id: str) -> list[Entry]: ...


class InMemorySagaRepository:
    """記憶體版：給單元測試。送出的命令放在 sent：(topic, 命令)。"""

    def __init__(self, topics: Mapping[str, str] = contracts.COMMAND_TOPICS) -> None:
        self._topics = dict(topics)
        self._sagas: dict[str, OrderSaga] = {}
        self._logs: dict[str, list[Entry]] = {}
        self._messages: set[str] = set()
        self.sent: list[tuple[str, contracts.Command]] = []

    def change(self, saga_id: str, apply: Apply, *, message_id: str | None = None) -> OrderSaga | None:
        if message_id is not None and message_id in self._messages:
            return None
        if message_id is not None:
            self._messages.add(message_id)
        current = self._sagas.get(saga_id)
        saga = apply(deepcopy(current) if current is not None else None)  # 改的是一份複本，像從資料庫讀出來的
        if saga is None:
            return None
        commands, entries = saga.pull()
        saga.version += 1
        self._sagas[saga_id] = saga
        self._logs.setdefault(saga_id, []).extend(entries)
        self.sent.extend((self._topics[c.receiver], c) for c in commands)
        return saga

    def get(self, saga_id: str) -> OrderSaga | None:
        return self._sagas.get(saga_id)

    def due(self, now: datetime) -> list[str]:
        return [s.saga_id for s in self._sagas.values() if s.deadline is not None and s.deadline <= now]

    def log(self, saga_id: str) -> list[Entry]:
        return list(self._logs.get(saga_id, []))
