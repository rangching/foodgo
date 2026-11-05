"""廚房單的 Repository。同一則訊息只能開一張廚房單：add() 連同訊息編號一起記下。"""
from typing import Protocol

from foodgo.kitchen.ticket import Ticket


class TicketRepository(Protocol):
    def next_id(self) -> str: ...

    def get(self, ticket_id: str) -> Ticket | None: ...

    def all(self) -> list[Ticket]: ...

    def seen(self, message_id: str) -> bool:
        """這則訊息處理過了沒？"""
        ...

    def add(self, ticket: Ticket, *, message_id: str | None = None) -> bool:
        """新增廚房單，並記下是哪一則訊息開的。這則訊息已經開過單了就什麼都不做，回 False。"""
        ...

    def save(self, ticket: Ticket) -> None:
        """存廚房單的新狀態（出餐）。"""
        ...


class InMemoryTicketRepository:
    """記憶體版：留給單元測試與第 3 集的行程內匯流排。PostgreSQL 版在 postgres.py。"""

    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._messages: set[str] = set()

    def next_id(self) -> str:
        return f"T{len(self._tickets) + 1:03d}"

    def get(self, ticket_id: str) -> Ticket | None:
        return self._tickets.get(ticket_id)

    def all(self) -> list[Ticket]:
        return list(self._tickets.values())

    def seen(self, message_id: str) -> bool:
        return message_id in self._messages

    def add(self, ticket: Ticket, *, message_id: str | None = None) -> bool:
        if message_id is not None:
            if message_id in self._messages:
                return False
            self._messages.add(message_id)
        self._tickets[ticket.ticket_id] = ticket
        return True

    def save(self, ticket: Ticket) -> None:
        self._tickets[ticket.ticket_id] = ticket
