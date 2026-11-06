"""廚房對外公告的事。格式由廚房負責維護，訂閱的人照這份讀。"""
from dataclasses import dataclass

from foodgo.contracts.base import IntegrationEvent

KITCHEN_TOPIC = "kitchen.events"  # 廚房的整合事件（第 7 集）；key 是 order_id


@dataclass(frozen=True, kw_only=True)
class MealReady(IntegrationEvent):
    """餐點已備妥：可以來取餐了。"""

    order_id: str
    ticket_id: str
    restaurant_id: str  # 到哪裡取餐


@dataclass(frozen=True, kw_only=True)
class TicketAccepted(IntegrationEvent):
    """餐廳已接單：這張單會做。外送可以開始找外送員了。"""

    order_id: str
    ticket_id: str


@dataclass(frozen=True, kw_only=True)
class TicketRejected(IntegrationEvent):
    """餐廳已拒單：這張單不做。"""

    order_id: str
    ticket_id: str
    reason: str
