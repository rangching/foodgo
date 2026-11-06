"""配送單的 Repository。外送也要記住「餐廳接單了沒」：廚房的事件和點餐的事件在不同的 topic，誰先到不一定。"""
from collections.abc import Sequence
from typing import Protocol

from foodgo import contracts
from foodgo.delivery.delivery import Delivery


class DeliveryRepository(Protocol):
    def next_id(self) -> str: ...

    def by_order(self, order_id: str) -> Delivery | None: ...

    def all(self) -> list[Delivery]: ...

    def seen(self, message_id: str) -> bool: ...

    def add(self, delivery: Delivery, *, message_id: str) -> bool:
        """新增配送單，並記下是哪一則訊息開的。處理過了就回 False。"""
        ...

    def save(self, delivery: Delivery, *, announce: Sequence[contracts.IntegrationEvent] = ()) -> None:
        """存新狀態；要公告的事件一起寫（PostgreSQL 版寫進 delivery_outbox，同一個交易）。"""
        ...

    def note_ticket(self, order_id: str, ticket_id: str, *, message_id: str) -> bool:
        """記下「餐廳接單了」。處理過了就回 False。"""
        ...

    def accepted_ticket(self, order_id: str) -> str | None:
        """接單的廚房單編號；還沒聽到是 None。"""
        ...


class InMemoryDeliveryRepository:
    """記憶體版：給單元測試。要公告的事件放在 announced。"""

    def __init__(self) -> None:
        self._deliveries: dict[str, Delivery] = {}
        self._messages: set[str] = set()
        self._tickets: dict[str, str] = {}
        self.announced: list[contracts.IntegrationEvent] = []

    def next_id(self) -> str:
        return f"D{len(self._deliveries) + 1:03d}"

    def by_order(self, order_id: str) -> Delivery | None:
        return next((d for d in self._deliveries.values() if d.order_id == order_id), None)

    def all(self) -> list[Delivery]:
        return list(self._deliveries.values())

    def seen(self, message_id: str) -> bool:
        return message_id in self._messages

    def add(self, delivery: Delivery, *, message_id: str) -> bool:
        if message_id in self._messages:
            return False
        self._messages.add(message_id)
        self._deliveries[delivery.delivery_id] = delivery
        return True

    def save(self, delivery: Delivery, *, announce: Sequence[contracts.IntegrationEvent] = ()) -> None:
        self._deliveries[delivery.delivery_id] = delivery
        self.announced.extend(announce)

    def note_ticket(self, order_id: str, ticket_id: str, *, message_id: str) -> bool:
        if message_id in self._messages:
            return False
        self._messages.add(message_id)
        self._tickets[order_id] = ticket_id
        return True

    def accepted_ticket(self, order_id: str) -> str | None:
        return self._tickets.get(order_id)
