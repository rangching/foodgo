"""廚房對外公告的事。格式由廚房負責維護，訂閱的人照這份讀。"""
from dataclasses import dataclass

from foodgo.contracts.base import IntegrationEvent


@dataclass(frozen=True, kw_only=True)
class MealReady(IntegrationEvent):
    """餐點已備妥：可以來取餐了。"""

    order_id: str
    ticket_id: str
    restaurant_id: str  # 到哪裡取餐
