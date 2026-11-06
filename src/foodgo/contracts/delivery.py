"""外送對外公告的事（第 7 集），與外送接受的命令（第 8 集）。格式由外送負責維護。"""
from dataclasses import dataclass
from typing import ClassVar

from foodgo.contracts.base import Command, IntegrationEvent

DELIVERY_TOPIC = "delivery.events"  # 外送的整合事件；key 是 order_id
DELIVERY_COMMANDS = "delivery.commands"  # 給外送的命令（第 8 集）；key 是 order_id


@dataclass(frozen=True, kw_only=True)
class CourierAssigned(IntegrationEvent):
    """外送員已指派：誰去取餐、送餐。"""

    order_id: str
    delivery_id: str
    courier_id: str


@dataclass(frozen=True, kw_only=True)
class AssignCourier(Command):
    """指派外送員（命令：編排器 → 外送）。餐廳接單了，從哪裡取、送到哪裡。回覆 CourierAssigned。"""

    receiver: ClassVar[str] = "delivery"

    order_id: str
    restaurant_id: str  # 取餐點
    delivery_address: str  # 送達點
