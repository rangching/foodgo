from dataclasses import dataclass

from foodgo.shared.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class OrderPlaced(DomainEvent):
    """訂單已送出"""

    order_id: str
    customer_id: str
    restaurant_id: str


@dataclass(frozen=True, kw_only=True)
class OrderCancelled(DomainEvent):
    """訂單已取消"""

    order_id: str
    reason: str
