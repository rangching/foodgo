"""廚房單的 Repository。同一則訊息只能開一張廚房單：add() 連同訊息編號一起記下。

第 7 集起廚房還要記住「這張訂單的付款怎麼了」：付款的事件和點餐的事件在不同的 topic，誰先到不一定。
"""
from collections.abc import Sequence
from typing import Protocol

from foodgo import contracts
from foodgo.kitchen.ticket import Ticket


class TicketRepository(Protocol):
    def next_id(self) -> str: ...

    def get(self, ticket_id: str) -> Ticket | None: ...

    def by_order(self, order_id: str) -> Ticket | None: ...

    def all(self) -> list[Ticket]: ...

    def seen(self, message_id: str) -> bool:
        """這則訊息處理過了沒？"""
        ...

    def add(
        self, ticket: Ticket, *, message_id: str | None = None, announce: Sequence[contracts.IntegrationEvent] = ()
    ) -> bool:
        """新增廚房單，並記下是哪一則訊息開的。這則訊息已經開過單了就什麼都不做，回 False。

        announce（第 8 集）：開單時就決定好的回覆（接單或拒單），一起寫進 kitchen_outbox。
        """
        ...

    def save(self, ticket: Ticket, *, announce: Sequence[contracts.IntegrationEvent] = ()) -> None:
        """存廚房單的新狀態；要公告的事件一起寫（PostgreSQL 版寫進 kitchen_outbox，同一個交易）。"""
        ...

    def note_payment(self, order_id: str, outcome: str, reason: str = "", *, message_id: str) -> bool:
        """記下聽到的付款結果（authorized／failed）。這則訊息處理過了就回 False。"""
        ...

    def payment_of(self, order_id: str) -> tuple[str, str] | None:
        """聽到的付款結果：(authorized／failed, 原因)；還沒聽到是 None。"""
        ...


class InMemoryTicketRepository:
    """記憶體版：留給單元測試與第 3 集的行程內匯流排。PostgreSQL 版在 postgres.py。"""

    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._messages: set[str] = set()
        self._payments: dict[str, tuple[str, str]] = {}
        self.announced: list[contracts.IntegrationEvent] = []  # PostgreSQL 版的 kitchen_outbox

    def next_id(self) -> str:
        return f"T{len(self._tickets) + 1:03d}"

    def get(self, ticket_id: str) -> Ticket | None:
        return self._tickets.get(ticket_id)

    def by_order(self, order_id: str) -> Ticket | None:
        return next((t for t in self._tickets.values() if t.order_id == order_id), None)

    def all(self) -> list[Ticket]:
        return list(self._tickets.values())

    def seen(self, message_id: str) -> bool:
        return message_id in self._messages

    def add(
        self, ticket: Ticket, *, message_id: str | None = None, announce: Sequence[contracts.IntegrationEvent] = ()
    ) -> bool:
        if message_id is not None:
            if message_id in self._messages:
                return False
            self._messages.add(message_id)
        self._tickets[ticket.ticket_id] = ticket
        self.announced.extend(announce)
        return True

    def save(self, ticket: Ticket, *, announce: Sequence[contracts.IntegrationEvent] = ()) -> None:
        self._tickets[ticket.ticket_id] = ticket
        self.announced.extend(announce)

    def note_payment(self, order_id: str, outcome: str, reason: str = "", *, message_id: str) -> bool:
        if message_id in self._messages:
            return False
        self._messages.add(message_id)
        self._payments[order_id] = (outcome, reason)
        return True

    def payment_of(self, order_id: str) -> tuple[str, str] | None:
        return self._payments.get(order_id)
