from dataclasses import dataclass
from typing import TYPE_CHECKING

from foodgo.shared.events import DomainEvent

if TYPE_CHECKING:  # 只給型別檢查看：model.py 也 import 這個檔，執行時不能互相 import
    from foodgo.ordering.model import Address, Money, OrderLine


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


# ---------------------------------------------------------------- 第 5 集：事件溯源要的事件
# Order 的每個動作都對應一個事件：建立草稿、加品項、送出、取消。
# 照順序把事件套上去（fold）就能算回整張訂單，所以建立時的資料、每個品項都要在事件裡。
@dataclass(frozen=True, kw_only=True)
class OrderDrafted(DomainEvent):
    """訂單草稿已建立"""

    order_id: str
    customer_id: str
    restaurant_id: str
    delivery_address: "Address"
    delivery_fee: "Money"
    note: str


@dataclass(frozen=True, kw_only=True)
class ItemAdded(DomainEvent):
    """品項已加入"""

    order_id: str
    name: str
    unit_price: "Money"
    quantity: int
