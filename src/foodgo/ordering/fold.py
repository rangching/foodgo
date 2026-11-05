"""事件溯源：訂單的狀態不另外存，用事件算出來——state = fold(events)。

兩個都是純函式：不碰資料庫、不看時鐘、不改舊的狀態，同樣的事件永遠算出同樣的訂單。
"""
from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import reduce

from foodgo.ordering.events import ItemAdded, OrderCancelled, OrderDrafted, OrderPlaced
from foodgo.ordering.model import Address, Money, OrderLine, OrderStatus
from foodgo.shared.events import DomainEvent


@dataclass(frozen=True)
class OrderState:
    """某一刻的訂單。version＝已經套用了幾個事件：第幾個事件之後的樣子。"""

    order_id: str
    customer_id: str
    restaurant_id: str
    delivery_address: Address
    delivery_fee: Money
    note: str
    lines: tuple[OrderLine, ...] = ()
    status: OrderStatus = OrderStatus.DRAFT
    version: int = 1  # 建立它的 OrderDrafted 是第 1 個事件

    @property
    def total(self) -> Money:
        return sum((line.subtotal for line in self.lines), self.delivery_fee)


def evolve(state: OrderState | None, event: DomainEvent) -> OrderState:
    """套上一個事件，回傳新的狀態。事件是已經發生的事實：這裡不檢查業務規則，照著做就好。"""
    if state is None:
        if not isinstance(event, OrderDrafted):
            raise ValueError(f"第一個事件要是 OrderDrafted，不是 {type(event).__name__}")
        return OrderState(
            event.order_id, event.customer_id, event.restaurant_id, event.delivery_address, event.delivery_fee, event.note
        )
    match event:
        case ItemAdded():
            state = replace(state, lines=(*state.lines, OrderLine(event.name, event.unit_price, event.quantity)))
        case OrderPlaced():  # 品項早就由 ItemAdded 加好了；它帶的 lines 是給廚房的快照
            state = replace(state, status=OrderStatus.PLACED)
        case OrderCancelled():
            state = replace(state, status=OrderStatus.CANCELLED)
        case OrderDrafted():
            raise ValueError(f"{state.order_id} 已經建立過了，不會再有第二個 OrderDrafted")
        case _:
            raise TypeError(f"fold 不認得這個事件：{type(event).__name__}")
    return replace(state, version=state.version + 1)


def fold(events: Iterable[DomainEvent]) -> OrderState | None:
    """從第一個事件套到最後一個。一個事件都沒有，就是沒有這張訂單（None）。"""
    return reduce(evolve, events, None)
