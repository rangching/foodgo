"""付款對外公告的事（第 7 集）。格式由付款負責維護。"""
from dataclasses import dataclass

from foodgo.contracts.base import IntegrationEvent

PAYMENT_TOPIC = "payment.events"  # 付款的整合事件；key 是 order_id


@dataclass(frozen=True, kw_only=True)
class PaymentAuthorized(IntegrationEvent):
    """付款已授權：錢沒問題，可以做菜了。不帶金額：廚房不看價格，退款時付款自己查得到。"""

    order_id: str
    payment_id: str


@dataclass(frozen=True, kw_only=True)
class PaymentFailed(IntegrationEvent):
    """付款已失敗：這張訂單收不到錢。"""

    order_id: str
    payment_id: str
    reason: str  # 例如「卡片額度不足」
