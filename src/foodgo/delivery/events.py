from dataclasses import dataclass

from foodgo.shared.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class CourierAssigned(DomainEvent):
    """外送員已指派"""

    delivery_id: str
    order_id: str
    courier_id: str


@dataclass(frozen=True, kw_only=True)
class MealPickedUp(DomainEvent):
    """餐點已取貨"""

    delivery_id: str
    order_id: str


@dataclass(frozen=True, kw_only=True)
class MealDelivered(DomainEvent):
    """餐點已送達"""

    delivery_id: str
    order_id: str
