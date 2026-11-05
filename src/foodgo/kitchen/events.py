from dataclasses import dataclass

from foodgo.shared.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class TicketAccepted(DomainEvent):
    """餐廳已接單"""

    ticket_id: str
    order_id: str


@dataclass(frozen=True, kw_only=True)
class TicketRejected(DomainEvent):
    """餐廳已拒單"""

    ticket_id: str
    order_id: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class MealReady(DomainEvent):
    """餐點已備妥"""

    ticket_id: str
    order_id: str
