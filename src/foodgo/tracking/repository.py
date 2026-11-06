"""訂單追蹤存在哪裡。查詢 API 只用 get 與 as_of_position；投影 consumer 用 PostgreSQL 版的 project。"""
from typing import Protocol

from foodgo import contracts
from foodgo.tracking.projection import Tracking, apply


class TrackingRepository(Protocol):
    def get(self, order_id: str) -> Tracking | None:
        """一張訂單的追蹤；投影還沒讀到就是 None。"""
        ...

    def as_of_position(self) -> int:
        """投影一共處理了幾則訊息：讀模型「新到哪裡」。"""
        ...


class InMemoryTrackingRepository:
    """記憶體版（第 3 集的匯流排模式、單元測試）：on_event 一收到就投影，沒有延遲。"""

    def __init__(self) -> None:
        self._rows: dict[str, Tracking] = {}
        self._handled = 0

    def on_event(self, event: contracts.IntegrationEvent) -> None:
        self._rows[event.order_id] = apply(self._rows.get(event.order_id), event)
        self._handled += 1

    def get(self, order_id: str) -> Tracking | None:
        return self._rows.get(order_id)

    def as_of_position(self) -> int:
        return self._handled

    def all(self) -> list[Tracking]:
        return [self._rows[order_id] for order_id in sorted(self._rows)]
