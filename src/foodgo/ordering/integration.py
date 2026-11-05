"""點餐的對外窗口：把內部的領域事件翻成共同語言（foodgo.contracts），再交出去。"""
from collections.abc import Callable

from foodgo import contracts
from foodgo.ordering.events import OrderPlaced
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
    return None  # OrderCancelled：目前沒有別的上下文要聽，先不公告


def publisher(send: Callable[[contracts.IntegrationEvent], None]) -> Callable[[DomainEvent], None]:
    """做一個給 API 用的 publish：先翻譯，再交給 send（這一集是事件匯流排）。"""

    def publish(event: DomainEvent) -> None:
        message = to_integration(event)
        if message is None:
            return
        print(f"點餐：發布 {type(message).__name__} {getattr(event, 'order_id', '')}", flush=True)
        send(message)

    return publish
