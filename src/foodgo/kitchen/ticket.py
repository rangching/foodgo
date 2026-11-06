from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class Dish:
    """要做的一道菜：菜名與份數。沒有價格。"""

    name: str
    quantity: int = 1


class TicketStatus(StrEnum):
    RECEIVED = "received"  # 已收單（第 7 集起：等付款）
    ACCEPTED = "accepted"  # 餐廳已接單
    REJECTED = "rejected"  # 餐廳已拒單
    CANCELLED = "cancelled"  # 不做了：付款失敗
    READY = "ready"  # 餐點已備妥


class TicketRuleViolation(Exception):
    """違反廚房單的規則。"""


@dataclass
class Ticket:
    """廚房單：廚房只在乎做哪些菜、有什麼備註，不看價格。"""

    ticket_id: str
    order_id: str  # 只記訂單編號，不認識點餐上下文的物件
    restaurant_id: str
    dishes: tuple[Dish, ...]
    note: str = ""
    status: TicketStatus = TicketStatus.RECEIVED

    # 第 7 集：接單、拒單、不做了，都只能從「已收單」開始
    def accept(self) -> None:
        self._still_received("接單")
        self.status = TicketStatus.ACCEPTED

    def reject(self) -> None:
        self._still_received("拒單")
        self.status = TicketStatus.REJECTED

    def cancel(self) -> None:
        self._still_received("取消")
        self.status = TicketStatus.CANCELLED

    def mark_ready(self) -> None:
        if self.status is TicketStatus.READY:
            raise TicketRuleViolation(f"廚房單 {self.ticket_id} 已經出餐了")
        if self.status in (TicketStatus.REJECTED, TicketStatus.CANCELLED):
            raise TicketRuleViolation(f"廚房單 {self.ticket_id} 不做了（{self.status.value}），不能出餐")
        self.status = TicketStatus.READY

    def _still_received(self, action: str) -> None:
        if self.status is not TicketStatus.RECEIVED:
            raise TicketRuleViolation(f"廚房單 {self.ticket_id} 已經是 {self.status.value}，不能{action}")
