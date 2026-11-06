"""點餐對外公告的事。格式由點餐負責維護，訂閱的人照這份讀。"""
from dataclasses import dataclass
from typing import Any, ClassVar, Self

from foodgo.contracts.base import Command, IntegrationEvent

ORDERING_TOPIC = "ordering.events"  # 點餐的整合事件都送到這個 Kafka topic；key 是 order_id
ORDERING_COMMANDS = "ordering.commands"  # 給點餐的命令（第 8 集）；key 是 order_id


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


# ---------------------------------------------------------------- 第 7 集：付款要金額、外送要地址
# 不加進 OrderPlaced：廚房也在讀它，加了廚房就會收到價格與顧客的地址。
# 送出訂單時另外公告兩則，各給一個上下文、只帶它要的（和 OrderPlaced 同一個交易寫進 outbox）。
# 第 8 集起（編排式）只有編排器聽這兩則：它收齊點餐的事實，再送真正的命令給付款、外送。
@dataclass(frozen=True, kw_only=True)
class PaymentRequested(IntegrationEvent):
    """付款已請求：這張訂單要付多少錢。給付款看，不帶菜名、不帶地址。"""

    order_id: str
    customer_id: str  # 向誰收錢
    amount: int  # 合計，新臺幣整數元（A001：230）


@dataclass(frozen=True, kw_only=True)
class DeliveryRequested(IntegrationEvent):
    """外送已請求：從哪裡取餐、送到哪裡。給外送看，不帶菜名、不帶價格。"""

    order_id: str
    restaurant_id: str  # 取餐點
    delivery_address: str  # 送達點


# ---------------------------------------------------------------- 第 8 集：取消要對外公告了
@dataclass(frozen=True, kw_only=True)
class OrderCancelled(IntegrationEvent):
    """訂單已取消：顧客自己取消，或編排器倒回去時要點餐取消。"""

    order_id: str
    reason: str  # 例如「餐廳拒單：餐廳太忙，暫停接單」


@dataclass(frozen=True, kw_only=True)
class CancelOrder(Command):
    """取消訂單（命令：編排器 → 點餐）。補償的最後一步。點餐做完公告 OrderCancelled。"""

    receiver: ClassVar[str] = "ordering"

    order_id: str
    reason: str
