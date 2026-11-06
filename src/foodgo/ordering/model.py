"""點餐的領域模型：規則寫在這裡，不寫在 API 裡。"""
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from foodgo.ordering.events import ItemAdded, OrderCancelled, OrderDrafted, OrderPlaced
from foodgo.shared.events import DomainEvent

if TYPE_CHECKING:  # 只給型別檢查看：fold.py 也 import 這個檔
    from foodgo.ordering.fold import OrderState


# ---------------------------------------------------------------- 值物件
@dataclass(frozen=True, order=True)
class Money:
    """金額：新臺幣，整數元。兩筆金額數字一樣就是同一個值。"""

    amount: int

    def __post_init__(self) -> None:
        if not isinstance(self.amount, int) or self.amount < 0:
            raise ValueError(f"金額要是 0 以上的整數元：{self.amount!r}")

    def __add__(self, other: "Money") -> "Money":
        return Money(self.amount + other.amount)

    def __mul__(self, times: int) -> "Money":
        return Money(self.amount * times)

    def __str__(self) -> str:
        return f"{self.amount} 元"


@dataclass(frozen=True)
class Address:
    """送達地址。地址一樣就是同一個地方，要換地址就換一個新的 Address。"""

    street: str

    def __post_init__(self) -> None:
        if not self.street.strip():
            raise ValueError("地址不能是空白")


@dataclass(frozen=True)
class OrderLine:
    """訂單上的一個品項：菜名、單價、數量。"""

    name: str
    unit_price: Money
    quantity: int = 1

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("菜名不能是空白")
        if self.quantity < 1:
            raise ValueError(f"數量至少是 1：{self.quantity}")

    @property
    def subtotal(self) -> Money:
        return self.unit_price * self.quantity


# ---------------------------------------------------------------- 聚合
class OrderStatus(StrEnum):
    DRAFT = "draft"  # 草稿：還在選菜
    PLACED = "placed"  # 已送出
    CANCELLED = "cancelled"  # 已取消


class OrderRuleViolation(Exception):
    """違反訂單的業務規則。"""


class Order:
    """訂單（聚合根）。外面只能透過它的方法改訂單，規則由它自己守。"""

    def __init__(
        self,
        *,
        order_id: str,
        customer_id: str,
        restaurant_id: str,
        delivery_address: Address,
        delivery_fee: Money,
        note: str = "",
    ) -> None:
        self.order_id = order_id
        self.customer_id = customer_id
        self.restaurant_id = restaurant_id
        self.delivery_address = delivery_address
        self.delivery_fee = delivery_fee
        self.note = note
        self._lines: list[OrderLine] = []
        self._status = OrderStatus.DRAFT
        self._events: list[DomainEvent] = []
        self._version = 0  # 套用了幾個事件（含還沒存的）：和 OrderState.version 同一個意思
        self._record(
            OrderDrafted(
                order_id=order_id,
                customer_id=customer_id,
                restaurant_id=restaurant_id,
                delivery_address=delivery_address,
                delivery_fee=delivery_fee,
                note=note,
            )
        )

    @classmethod
    def from_state(cls, state: "OrderState") -> "Order":
        """給 Repository 用：把 fold(events) 算出來的狀態擺回聚合。不檢查規則、不記事件——事情早就發生過了。"""
        order = cls(
            order_id=state.order_id,
            customer_id=state.customer_id,
            restaurant_id=state.restaurant_id,
            delivery_address=state.delivery_address,
            delivery_fee=state.delivery_fee,
            note=state.note,
        )
        order._lines = list(state.lines)
        order._status = state.status
        order._version = state.version
        order._events = []  # 建構時記下的 OrderDrafted 不算：它早就在事件流裡了
        return order

    # 實體：靠編號認人，不看內容
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Order) and other.order_id == self.order_id

    def __hash__(self) -> int:
        return hash(self.order_id)

    @property
    def status(self) -> OrderStatus:
        return self._status

    @property
    def version(self) -> int:
        """第幾版：套用了幾個事件。存的時候用「版本 − 還沒存的事件數」當 expected_version。"""
        return self._version

    @property
    def lines(self) -> tuple[OrderLine, ...]:
        return tuple(self._lines)

    @property
    def subtotal(self) -> Money:
        return sum((line.subtotal for line in self._lines), Money(0))

    @property
    def total(self) -> Money:
        return self.subtotal + self.delivery_fee

    def add_line(self, name: str, unit_price: Money, quantity: int = 1) -> None:
        if self._status is not OrderStatus.DRAFT:
            raise OrderRuleViolation("訂單送出後不能再改品項")
        self._lines.append(OrderLine(name, unit_price, quantity))
        self._record(ItemAdded(order_id=self.order_id, name=name, unit_price=unit_price, quantity=quantity))

    def place(self) -> None:
        if self._status is not OrderStatus.DRAFT:
            raise OrderRuleViolation("訂單已經送出過了")
        if not self._lines:
            raise OrderRuleViolation("訂單至少要有一個品項")
        self._status = OrderStatus.PLACED
        self._record(
            OrderPlaced(
                order_id=self.order_id,
                customer_id=self.customer_id,
                restaurant_id=self.restaurant_id,
                lines=self.lines,
                note=self.note,
            )
        )

    def cancel(self, reason: str) -> None:
        if self._status is OrderStatus.DRAFT:
            raise OrderRuleViolation("訂單還沒送出，不能取消")
        if self._status is OrderStatus.CANCELLED:
            raise OrderRuleViolation("訂單已取消，不能再取消")
        if not reason.strip():
            raise OrderRuleViolation("取消要寫理由")
        self._status = OrderStatus.CANCELLED
        self._record(OrderCancelled(order_id=self.order_id, reason=reason))

    # 領域事件：聚合先記下來，由外面（Repository）一次取走
    def _record(self, event: DomainEvent) -> None:
        self._events.append(event)
        self._version += 1

    def pull_events(self) -> list[DomainEvent]:
        events, self._events = self._events, []
        return events
