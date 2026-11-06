"""廚房對外公告的事，與廚房接受的命令（第 8 集）。格式由廚房負責維護，訂閱的人照這份讀。"""
from dataclasses import dataclass
from typing import Any, ClassVar, Self

from foodgo.contracts.base import Command, IntegrationEvent
from foodgo.contracts.ordering import OrderItem

KITCHEN_TOPIC = "kitchen.events"  # 廚房的整合事件（第 7 集）；key 是 order_id
KITCHEN_COMMANDS = "kitchen.commands"  # 給廚房的命令（第 8 集）；key 是 order_id


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


# ---------------------------------------------------------------- 第 8 集：命令
@dataclass(frozen=True, kw_only=True)
class AcceptTicket(Command):
    """接單（命令：編排器 → 廚房）。付款已經授權了，請餐廳開單、決定接不接。回覆 TicketAccepted 或 TicketRejected。

    帶著要做的菜（和 OrderPlaced 一樣不帶價格）：廚房只聽自己的命令 topic，不用再去等別人的事件。
    """

    receiver: ClassVar[str] = "kitchen"

    order_id: str
    restaurant_id: str
    items: tuple[OrderItem, ...]
    note: str = ""

    @classmethod
    def _from_data(cls, *, items: list[dict[str, Any]], **data: Any) -> Self:
        return cls(items=tuple(OrderItem(name=i["name"], quantity=i["quantity"]) for i in items), **data)


@dataclass(frozen=True, kw_only=True)
class CancelTicket(Command):
    """取消廚房單（補償命令：編排器 → 廚房）。已經出餐的單取消不了。回覆 TicketCancelled。"""

    receiver: ClassVar[str] = "kitchen"

    order_id: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class TicketCancelled(IntegrationEvent):
    """廚房單已取消：這張單不做了。"""

    order_id: str
    ticket_id: str
    reason: str
