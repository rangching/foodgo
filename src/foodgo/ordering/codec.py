"""點餐的事件、狀態 ↔ JSON：events 表只存 type 和 data，哪個 type 是哪個類別、值物件怎麼攤平，由點餐自己決定。

純函式：不碰資料庫。事件存下去就不會改，這裡要一直讀得懂舊的 data（第 3 集的版本規則在這裡又出現一次）。
"""
from dataclasses import fields
from datetime import datetime
from typing import Any
from uuid import UUID

from foodgo.ordering.events import ItemAdded, OrderCancelled, OrderDrafted, OrderPlaced
from foodgo.ordering.fold import OrderState
from foodgo.ordering.model import Address, Money, OrderLine, OrderStatus
from foodgo.shared.events import DomainEvent

EVENT_TYPES: dict[str, type[DomainEvent]] = {
    cls.__name__: cls for cls in (OrderDrafted, ItemAdded, OrderPlaced, OrderCancelled)
}
META = ("event_id", "occurred_at")  # 每個事件都有；存在 events 表自己的欄位，不放進 data


def plain(value: Any) -> Any:
    """值物件 → JSON 認得的型別：Money → 整數元、Address → 字串、品項 → dict。"""
    match value:
        case Money():
            return value.amount
        case Address():
            return value.street
        case OrderLine():
            return {"name": value.name, "unit_price": value.unit_price.amount, "quantity": value.quantity}
        case OrderStatus():
            return value.value
        case tuple() | list():
            return [plain(item) for item in value]
    return value


def line_of(data: dict[str, Any]) -> OrderLine:
    return OrderLine(data["name"], Money(data["unit_price"]), data["quantity"])


# ---------------------------------------------------------------- 事件 ↔ (type, data)
def to_data(event: DomainEvent) -> tuple[str, dict[str, Any]]:
    """事件 → events 表的 type 和 data。"""
    kind = type(event).__name__
    if kind not in EVENT_TYPES:
        raise TypeError(f"點餐不認得這個事件：{kind}")
    return kind, {f.name: plain(getattr(event, f.name)) for f in fields(event) if f.name not in META}


def from_data(kind: str, data: dict[str, Any], *, event_id: UUID, occurred_at: datetime) -> DomainEvent:
    """events 表的一列 → 事件。認不得的 type 丟 TypeError，不默默略過。"""
    meta = {"event_id": event_id, "occurred_at": occurred_at}
    match kind:
        case "OrderDrafted":
            return OrderDrafted(
                **meta,
                order_id=data["order_id"],
                customer_id=data["customer_id"],
                restaurant_id=data["restaurant_id"],
                delivery_address=Address(data["delivery_address"]),
                delivery_fee=Money(data["delivery_fee"]),
                note=data["note"],
            )
        case "ItemAdded":
            return ItemAdded(
                **meta,
                order_id=data["order_id"],
                name=data["name"],
                unit_price=Money(data["unit_price"]),
                quantity=data["quantity"],
            )
        case "OrderPlaced":
            return OrderPlaced(
                **meta,
                order_id=data["order_id"],
                customer_id=data["customer_id"],
                restaurant_id=data["restaurant_id"],
                lines=tuple(line_of(line) for line in data["lines"]),
                note=data["note"],
            )
        case "OrderCancelled":
            return OrderCancelled(**meta, order_id=data["order_id"], reason=data["reason"])
    raise TypeError(f"點餐不認得這個事件：{kind}")


# ---------------------------------------------------------------- 狀態 ↔ 快照
def state_to_data(state: OrderState) -> dict[str, Any]:
    """OrderState → snapshots 表的 state。"""
    return {f.name: plain(getattr(state, f.name)) for f in fields(state)}


def state_from_data(data: dict[str, Any]) -> OrderState:
    """snapshots 表的 state → OrderState。"""
    return OrderState(
        order_id=data["order_id"],
        customer_id=data["customer_id"],
        restaurant_id=data["restaurant_id"],
        delivery_address=Address(data["delivery_address"]),
        delivery_fee=Money(data["delivery_fee"]),
        note=data["note"],
        lines=tuple(line_of(line) for line in data["lines"]),
        status=OrderStatus(data["status"]),
        version=data["version"],
    )
