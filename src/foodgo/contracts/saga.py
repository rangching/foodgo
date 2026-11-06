"""編排器對外公告的事（第 9 集）。格式由編排器負責維護，訂閱的人照這份讀。

編排器平常不公告事件：付款、廚房、外送、點餐做了什麼，各自公告了。
只有一件事只有它知道——「我放棄了，這張訂單要有人接手」——所以只公告這一則。
"""
from dataclasses import dataclass

from foodgo.contracts.base import IntegrationEvent

SAGA_TOPIC = "saga.events"  # 編排器的整合事件（第 9 集）；key 是 order_id。和別人一樣：<發布者>.events


@dataclass(frozen=True, kw_only=True)
class SagaEscalated(IntegrationEvent):
    """已轉人工處理：補償做不下去（例如退款一直失敗），編排器停下來，等人接手。"""

    order_id: str
    reason: str  # 為什麼放棄，例如「RefundPayment 試了 2 次都失敗（發卡銀行暫時無法退款）」
