"""配送單：從哪裡取餐、送到哪裡、誰來送。不知道菜名、不知道價格。"""
from dataclasses import dataclass
from enum import StrEnum


class DeliveryStatus(StrEnum):
    WAITING = "waiting"  # 等餐廳接單
    ASSIGNED = "assigned"  # 外送員已指派


class DeliveryRuleViolation(Exception):
    """違反配送單的規則。"""


@dataclass
class Delivery:
    delivery_id: str
    order_id: str  # 只記訂單編號
    pickup: str  # 取餐點：餐廳編號
    dropoff: str  # 送達點：地址
    status: DeliveryStatus = DeliveryStatus.WAITING
    courier_id: str = ""

    def assign(self, courier_id: str) -> None:
        if self.status is not DeliveryStatus.WAITING:
            raise DeliveryRuleViolation(f"配送單 {self.delivery_id} 已經指派給 {self.courier_id} 了")
        self.courier_id = courier_id
        self.status = DeliveryStatus.ASSIGNED
