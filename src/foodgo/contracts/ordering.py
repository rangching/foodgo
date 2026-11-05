"""點餐對外公告的事。格式由點餐負責維護，訂閱的人照這份讀。"""
from dataclasses import dataclass
from typing import Any, Self

from foodgo.contracts.base import IntegrationEvent

ORDERING_TOPIC = "ordering.events"  # 點餐的整合事件都送到這個 Kafka topic；key 是 order_id


@dataclass(frozen=True, kw_only=True)
class OrderItem:
    """要做的一個品項：菜名與份數。刻意不放單價。"""

    name: str
    quantity: int


@dataclass(frozen=True, kw_only=True)
class OrderPlaced(IntegrationEvent):
    """訂單已送出：可以開始準備這張訂單了。"""

    order_id: str
    restaurant_id: str
    items: tuple[OrderItem, ...]
    note: str = ""

    @classmethod
    def _from_data(cls, *, items: list[dict[str, Any]], **data: Any) -> Self:
        return cls(items=tuple(OrderItem(name=i["name"], quantity=i["quantity"]) for i in items), **data)
