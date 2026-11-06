"""訂單的 Repository：模型只認這個介面，存在哪裡是外面的事。"""
from collections.abc import Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from foodgo.ordering.fold import OrderState
from foodgo.ordering.model import Order
from foodgo.shared.event_store import RecordedEvent
from foodgo.shared.events import DomainEvent


class OrderRepository(Protocol):
    def next_id(self) -> str: ...

    def get(self, order_id: str) -> Order | None: ...

    def save(self, order: Order) -> None:
        """存訂單，並交出聚合身上的新事件（pull_events）。PostgreSQL 版兩件事在同一個交易。

        事件溯源版（第 6 集）：有人先寫了這張訂單，丟 ConcurrencyConflict（foodgo.shared.event_store）。
        """
        ...


@runtime_checkable
class OrderHistory(Protocol):
    """記得過去的 Repository（第 6 集的事件溯源版）：時光機、每一個事件。記憶體版沒有。"""

    def load_as_of(self, order_id: str, when: datetime) -> OrderState | None: ...

    def history(self, order_id: str) -> list[RecordedEvent]: ...


class InMemoryOrderRepository:
    """記憶體版：程式一關就不見。留給單元測試與不開 Docker 的時候；PostgreSQL 版在 event_sourcing.py（第 6 集起）。"""

    def __init__(self, publish: Callable[[DomainEvent], None] | None = None) -> None:
        self._orders: dict[str, Order] = {}
        self._count = 0
        self._publish = publish or (lambda event: None)

    def next_id(self) -> str:
        self._count += 1
        return f"A{self._count:03d}"

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def save(self, order: Order) -> None:
        self._orders[order.order_id] = order
        for event in order.pull_events():
            self._publish(event)
