"""付款：一張訂單一筆。金額是新臺幣整數元，和點餐用同一個單位（翻譯成金流服務的單位是防腐層的事）。"""
from dataclasses import dataclass
from enum import StrEnum


class PaymentStatus(StrEnum):
    AUTHORIZED = "authorized"  # 付款已授權
    FAILED = "failed"  # 付款已失敗


@dataclass(frozen=True)
class Payment:
    """付款。只記訂單編號與金額，不知道點了哪些菜。"""

    payment_id: str
    order_id: str
    customer_id: str
    amount: int  # 新臺幣，元
    status: PaymentStatus
    reference: str = ""  # 金流服務給的授權碼
    reason: str = ""  # 失敗的原因（我們的話）
