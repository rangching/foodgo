from dataclasses import dataclass

from foodgo.shared.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class PaymentAuthorized(DomainEvent):
    """付款已授權"""

    payment_id: str
    order_id: str
    amount: int  # 新臺幣，元


@dataclass(frozen=True, kw_only=True)
class PaymentFailed(DomainEvent):
    """付款已失敗"""

    payment_id: str
    order_id: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class PaymentRefunded(DomainEvent):
    """付款已退款"""

    payment_id: str
    order_id: str
    amount: int
