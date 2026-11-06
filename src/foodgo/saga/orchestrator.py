"""編排器的應用服務：收到的事件交給對的 OrderSaga，時間到了叫它檢查逾時。

它只認共同語言：點餐的三則事實（出發）、每個參與者的回覆。其他事件（例如 MealReady）略過。
"""
import json
from collections.abc import Callable
from datetime import UTC, datetime

from foodgo import contracts
from foodgo.saga.machine import FACTS, OrderSaga, Settings
from foodgo.saga.repository import InMemorySagaRepository, SagaRepository

# 編排器聽得懂的事件：點餐的事實＋參與者的回覆
HEARS: dict[str, type[contracts.IntegrationEvent]] = {
    cls.__name__: cls
    for cls in (
        contracts.OrderPlaced,
        contracts.PaymentRequested,
        contracts.DeliveryRequested,
        contracts.OrderCancelled,
        contracts.PaymentAuthorized,
        contracts.PaymentFailed,
        contracts.PaymentRefunded,
        contracts.RefundFailed,
        contracts.TicketAccepted,
        contracts.TicketRejected,
        contracts.TicketCancelled,
        contracts.CourierAssigned,
    )
}


def now_utc() -> datetime:
    return datetime.now(UTC)


class Orchestrator:
    def __init__(
        self,
        sagas: SagaRepository | None = None,
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._sagas = sagas or InMemorySagaRepository()
        self._settings = settings or Settings()
        self._clock = clock  # 現在幾點：測試給一個假的時鐘，不用真的等

    def get(self, saga_id: str) -> OrderSaga | None:
        return self._sagas.get(saga_id)

    def on_payload(self, payload: str) -> OrderSaga | None:
        """Kafka 來的一則 JSON：聽得懂就處理，聽不懂就略過。"""
        kind = json.loads(payload)["type"]
        if kind not in HEARS:
            return None
        return self.on_event(HEARS[kind].from_json(payload))

    def on_event(self, message: contracts.IntegrationEvent) -> OrderSaga | None:
        """一則事件：點餐的事實就開（或接著收）saga；參與者的回覆就交給那張訂單的 saga。同一則只處理一次。"""
        kind, order_id, now = type(message).__name__, message.order_id, self._clock()

        def apply(saga: OrderSaga | None) -> OrderSaga | None:
            if saga is None:
                if kind not in FACTS:
                    print(f"編排器：{order_id} 沒有 saga，{kind} 略過", flush=True)
                    return None
                saga = OrderSaga(order_id)
            saga.receive(message, now, self._settings)
            _say(saga)
            return saga

        return self._sagas.change(order_id, apply, message_id=message.event_id)

    def tick(self) -> None:
        """看看有沒有等到時間到了的 saga：有就重送或放棄（consumer 每處理完一則、每閒一秒呼叫一次）。"""
        now = self._clock()
        for saga_id in self._sagas.due(now):

            def apply(saga: OrderSaga | None) -> OrderSaga | None:
                if saga is None:
                    return None
                saga.check_deadline(now, self._settings)
                _say(saga)
                return saga

            self._sagas.change(saga_id, apply)


def _say(saga: OrderSaga) -> None:
    for entry in saga.written:
        what = " ".join(part for part in (entry.action, entry.message) if part)
        detail = f"：{entry.detail}" if entry.detail else ""
        print(f"編排器：{saga.saga_id} {entry.state.value:<16} {what}{detail}", flush=True)
