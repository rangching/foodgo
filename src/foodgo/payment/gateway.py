"""防腐層（Anti-Corruption Layer）：付款說自己的話，金流服務說它的話，翻譯只在這裡。

    我們：authorize("A001", 230) → Authorization(approved=True, reference="AUTH-0001")
    他們：{"merchant_order_no": "A001", "amount_minor": 23000, "currency": "TWD"} → {"result": "APPROVED", …}

換一家金流服務，只要換掉這裡的 adapter；付款的模型和服務一行都不用改。
第 8 集多了退款：refund("A001", 230) → Refund(approved=True, reference="RF-0001")。
"""
import os
from dataclasses import dataclass
from typing import Protocol

from foodgo.payment.fakepay import FakePay


@dataclass(frozen=True)
class Authorization:
    """授權的結果，用付款自己的話說。"""

    approved: bool
    reference: str = ""  # 授權碼
    reason: str = ""  # 沒過的原因


@dataclass(frozen=True)
class Refund:
    """退款的結果（第 8 集），用付款自己的話說。"""

    approved: bool
    reference: str = ""  # 退款編號
    reason: str = ""  # 退不了的原因


class CardGateway(Protocol):
    """付款要的動作：授權，與退款（第 8 集）。"""

    def authorize(self, order_id: str, amount: int) -> Authorization: ...

    def refund(self, order_id: str, amount: int) -> Refund: ...


DECLINE_REASONS = {"insufficient_funds": "卡片額度不足", "expired_card": "卡片已過期"}
REFUND_ERRORS = {"issuer_unavailable": "發卡銀行暫時無法退款"}


class FakePayAdapter:
    """把「授權 230 元」翻成 FakePay 的請求，再把它的回應翻回 Authorization。退款也一樣。"""

    def __init__(self, client: FakePay) -> None:
        self._client = client

    def authorize(self, order_id: str, amount: int) -> Authorization:
        answer = self._client.create_authorization(
            {"merchant_order_no": order_id, "amount_minor": amount * 100, "currency": "TWD"}  # 元 → 分
        )  # 用訂單編號當冪等鍵：同一張訂單重送幾次，都只授權一次
        if answer["result"] == "APPROVED":
            return Authorization(approved=True, reference=answer["auth_code"])
        code = answer["decline_code"]
        return Authorization(approved=False, reason=DECLINE_REASONS.get(code, f"金流服務拒絕（{code}）"))

    def refund(self, order_id: str, amount: int) -> Refund:
        answer = self._client.create_refund(
            {"merchant_order_no": order_id, "amount_minor": amount * 100, "currency": "TWD"}
        )  # 一樣用訂單編號當冪等鍵：重試退款不會退兩次
        if answer["result"] == "REFUNDED":
            return Refund(approved=True, reference=answer["refund_no"])
        code = answer["error_code"]
        return Refund(approved=False, reason=REFUND_ERRORS.get(code, f"金流服務退款失敗（{code}）"))


def from_environment() -> FakePayAdapter:
    """consumer 啟動時用這個：FOODGO_FAULT_PAYMENT=decline 讓每一筆都被拒絕；FOODGO_FAULT_REFUND=fail 讓退款都失敗。"""
    return FakePayAdapter(
        FakePay(fault=os.environ.get("FOODGO_FAULT_PAYMENT", ""), refund_fault=os.environ.get("FOODGO_FAULT_REFUND", ""))
    )
