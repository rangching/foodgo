"""訂單的 Repository：模型只認這個介面，存在哪裡是外面的事。"""
from typing import Protocol

from foodgo.ordering.model import Order


class OrderRepository(Protocol):
    def next_id(self) -> str: ...

    def get(self, order_id: str) -> Order | None: ...

    def save(self, order: Order) -> None: ...


class InMemoryOrderRepository:
    """先用記憶體版：程式一關就不見。第 4 集換成 PostgreSQL。"""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._count = 0

    def next_id(self) -> str:
        self._count += 1
        return f"A{self._count:03d}"

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def save(self, order: Order) -> None:
        self._orders[order.order_id] = order
