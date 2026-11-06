"""假的金流服務：假裝這是別家公司的 SDK。欄位名稱、金額單位、結果代碼都是「他們的」，我們改不了。

付款的程式只透過 gateway.py（防腐層）跟它說話；模型與服務直接 import 這個檔，會被第 7 條邊界規則擋下。
FOODGO_FAULT_PAYMENT=decline：每一筆都拒絕（卡片額度不足），示範付款失敗。
"""
from typing import Any


class FakePay:
    def __init__(self, *, fault: str = "") -> None:
        self._fault = fault
        self._answers: dict[str, dict[str, Any]] = {}  # 真的金流服務會存在他們那邊；假的只記在記憶體

    def create_authorization(self, request: dict[str, Any]) -> dict[str, Any]:
        """他們的 API：金額用「分」（amount_minor），結果是 APPROVED／DECLINED。

        同一個 merchant_order_no 再送一次，回上一次的答案，不會刷兩次卡（冪等鍵）。
        """
        key = request["merchant_order_no"]
        if key not in self._answers:
            if self._fault == "decline":
                answer = {"result": "DECLINED", "auth_code": None, "decline_code": "insufficient_funds"}
            else:
                answer = {"result": "APPROVED", "auth_code": f"AUTH-{len(self._answers) + 1:04d}", "decline_code": None}
            self._answers[key] = {**answer, "amount_minor": request["amount_minor"], "currency": request["currency"]}
        return self._answers[key]
