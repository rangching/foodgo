from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class Dish:
    """要做的一道菜：菜名與份數。沒有價格。"""

    name: str
    quantity: int = 1


class TicketStatus(StrEnum):
    RECEIVED = "received"  # 已收單
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

    def mark_ready(self) -> None:
        if self.status is TicketStatus.READY:
            raise TicketRuleViolation(f"廚房單 {self.ticket_id} 已經出餐了")
        self.status = TicketStatus.READY
