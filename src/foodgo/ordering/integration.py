"""點餐的對外窗口：把內部的領域事件翻成共同語言（foodgo.contracts），再交出去。"""
from collections.abc import Callable, Iterable
from uuid import uuid5

from foodgo import contracts
from foodgo.ordering.events import OrderCancelled, OrderPlaced
from foodgo.ordering.model import Order
from foodgo.shared.events import DomainEvent


def to_integration(event: DomainEvent) -> contracts.IntegrationEvent | None:
    """領域事件 → 整合事件。不對外公告的事件回 None。"""
    if isinstance(event, OrderPlaced):
        return contracts.OrderPlaced(
            event_id=str(event.event_id),  # 同一件事，同一個編號與時間
            occurred_at=event.occurred_at.isoformat(timespec="seconds"),
            order_id=event.order_id,
            restaurant_id=event.restaurant_id,
            items=tuple(contracts.OrderItem(name=line.name, quantity=line.quantity) for line in event.lines),
            note=event.note,
        )  # 單價、外送費、顧客、地址都留在點餐裡
    return None  # OrderCancelled：第 3 集的匯流排、第 4 集的 CRUD 版不公告；第 8 集起由 announcements() 公告


def announcements(order: Order, events: Iterable[DomainEvent]) -> list[contracts.IntegrationEvent]:
    """存檔時要對外公告的整合事件（第 7 集起）：送出訂單時一次公告三則，各給一個上下文、只帶它要的。

      OrderPlaced        → 廚房：做哪些菜（第 3 集那一則，沒有變）
      PaymentRequested   → 付款：向誰收、收多少
      DeliveryRequested  → 外送：從哪裡取、送到哪裡

    金額、地址在送出之後就不會再變（訂單的不變條件），所以用存檔時的訂單來翻譯。
    另外兩則的編號由 OrderPlaced 的編號算出來（uuid5）：同一件事翻幾次都是同一組編號。

    第 8 集起取消也公告：OrderCancelled（顧客取消，或編排器送 CancelOrder 來）。
    """
    messages: list[contracts.IntegrationEvent] = []
    for event in events:
        if isinstance(event, OrderCancelled):
            messages.append(
                contracts.OrderCancelled(
                    event_id=str(event.event_id),  # 同一件事，同一個編號與時間
                    occurred_at=event.occurred_at.isoformat(timespec="seconds"),
                    order_id=event.order_id,
                    reason=event.reason,
                )
            )
            continue
        message = to_integration(event)
        if message is None:
            continue
        messages.append(message)
        if isinstance(event, OrderPlaced):
            messages.append(
                contracts.PaymentRequested(
                    event_id=str(uuid5(event.event_id, "PaymentRequested")),
                    occurred_at=message.occurred_at,
                    order_id=order.order_id,
                    customer_id=order.customer_id,
                    amount=order.total.amount,
                )
            )
            messages.append(
                contracts.DeliveryRequested(
                    event_id=str(uuid5(event.event_id, "DeliveryRequested")),
                    occurred_at=message.occurred_at,
                    order_id=order.order_id,
                    restaurant_id=order.restaurant_id,
                    delivery_address=order.delivery_address.street,
                )
            )
    return messages


def publisher(send: Callable[[contracts.IntegrationEvent], None]) -> Callable[[DomainEvent], None]:
    """做一個給 API 用的 publish：先翻譯，再交給 send（第 3 集的事件匯流排；只有給廚房的 OrderPlaced）。"""

    def publish(event: DomainEvent) -> None:
        message = to_integration(event)
        if message is None:
            return
        print(f"點餐：發布 {type(message).__name__} {getattr(event, 'order_id', '')}", flush=True)
        send(message)

    return publish
