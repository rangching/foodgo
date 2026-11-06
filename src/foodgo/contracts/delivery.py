"""外送對外公告的事（第 7 集）。格式由外送負責維護。"""
from dataclasses import dataclass

from foodgo.contracts.base import IntegrationEvent

DELIVERY_TOPIC = "delivery.events"  # 外送的整合事件；key 是 order_id


@dataclass(frozen=True, kw_only=True)
class CourierAssigned(IntegrationEvent):
    """外送員已指派：誰去取餐、送餐。"""

    order_id: str
    delivery_id: str
    courier_id: str
