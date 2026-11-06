"""付款對外公告的事（第 7 集），與付款接受的命令（第 8 集）。格式由付款負責維護。"""
from dataclasses import dataclass
from typing import ClassVar

from foodgo.contracts.base import Command, IntegrationEvent

PAYMENT_TOPIC = "payment.events"  # 付款的整合事件；key 是 order_id
PAYMENT_COMMANDS = "payment.commands"  # 給付款的命令（第 8 集）；key 是 order_id


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


# ---------------------------------------------------------------- 第 8 集：命令與退款
@dataclass(frozen=True, kw_only=True)
class AuthorizePayment(Command):
    """授權付款（命令：編排器 → 付款）。回覆 PaymentAuthorized 或 PaymentFailed。"""

    receiver: ClassVar[str] = "payment"

    order_id: str
    customer_id: str  # 向誰收錢
    amount: int  # 新臺幣整數元


@dataclass(frozen=True, kw_only=True)
class RefundPayment(Command):
    """退款（補償命令：編排器 → 付款）。不帶金額：付款自己知道授權了多少。回覆 PaymentRefunded 或 RefundFailed。"""

    receiver: ClassVar[str] = "payment"

    order_id: str
    reason: str  # 為什麼要退，例如「餐廳拒單：餐廳太忙，暫停接單」


@dataclass(frozen=True, kw_only=True)
class PaymentRefunded(IntegrationEvent):
    """付款已退款：授權的錢還給顧客了（11 個領域事件裡就有的那一個）。"""

    order_id: str
    payment_id: str
    amount: int  # 退了多少元；沒有扣到款就是 0


@dataclass(frozen=True, kw_only=True)
class RefundFailed(IntegrationEvent):
    """退款已失敗：金流服務這次退不了。補償本身也會失敗。"""

    order_id: str
    payment_id: str
    reason: str  # 例如「發卡銀行暫時無法退款」
