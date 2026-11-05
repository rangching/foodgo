from dataclasses import dataclass
from typing import TYPE_CHECKING

from foodgo.shared.events import DomainEvent

if TYPE_CHECKING:  # 只給型別檢查看：model.py 也 import 這個檔，執行時不能互相 import
    from foodgo.ordering.model import OrderLine


@dataclass(frozen=True, kw_only=True)
class OrderPlaced(DomainEvent):
    """訂單已送出"""

    order_id: str
    customer_id: str
    restaurant_id: str
    lines: tuple["OrderLine", ...]  # 送出了哪些品項（含單價：點餐自己人看的）
    note: str


@dataclass(frozen=True, kw_only=True)
class OrderCancelled(DomainEvent):
    """訂單已取消"""

    order_id: str
    reason: str
