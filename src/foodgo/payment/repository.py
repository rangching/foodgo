"""付款的 Repository。記下一筆付款、記下「這則訊息處理過了」、要公告的事件：同一個交易。

第 8 集多了 save()：付款的狀態會變（退款），也要能「什麼都不改、再回一次」（編排器重試時）。
"""
from collections.abc import Sequence
from typing import Protocol

from foodgo import contracts
from foodgo.payment.payment import Payment


class PaymentRepository(Protocol):
    def next_id(self) -> str: ...

    def seen(self, message_id: str) -> bool: ...

    def by_order(self, order_id: str) -> Payment | None: ...

    def all(self) -> list[Payment]: ...

    def add(self, payment: Payment, *, message_id: str, announce: Sequence[contracts.IntegrationEvent] = ()) -> bool:
        """記下付款與要公告的事件（PostgreSQL 版寫進 payment_outbox）。這則訊息處理過了就什麼都不做，回 False。"""
        ...

    def save(
        self, payment: Payment | None, *, message_id: str, announce: Sequence[contracts.IntegrationEvent] = ()
    ) -> bool:
        """第 8 集：存付款的新樣子（沒有就新增；None＝不改付款），連同要公告的事件。這則訊息處理過了就回 False。"""
        ...


class InMemoryPaymentRepository:
    """記憶體版：給單元測試。要公告的事件放在 announced，看得到就好。"""

    def __init__(self) -> None:
        self._payments: dict[str, Payment] = {}
        self._messages: set[str] = set()
        self.announced: list[contracts.IntegrationEvent] = []

    def next_id(self) -> str:
        return f"P{len(self._payments) + 1:03d}"

    def seen(self, message_id: str) -> bool:
        return message_id in self._messages

    def by_order(self, order_id: str) -> Payment | None:
        return next((p for p in self._payments.values() if p.order_id == order_id), None)

    def all(self) -> list[Payment]:
        return list(self._payments.values())

    def add(self, payment: Payment, *, message_id: str, announce: Sequence[contracts.IntegrationEvent] = ()) -> bool:
        if message_id in self._messages:
            return False
        self._messages.add(message_id)
        self._payments[payment.payment_id] = payment
        self.announced.extend(announce)
        return True

    def save(
        self, payment: Payment | None, *, message_id: str, announce: Sequence[contracts.IntegrationEvent] = ()
    ) -> bool:
        if message_id in self._messages:
            return False
        self._messages.add(message_id)
        if payment is not None:
            self._payments[payment.payment_id] = payment
        self.announced.extend(announce)
        return True
