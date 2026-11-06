"""外送的應用服務（派車台）：先開配送單等著，餐廳接單了才指派外送員。"""
from collections.abc import Sequence

from foodgo import contracts
from foodgo.delivery.delivery import Delivery, DeliveryStatus
from foodgo.delivery.repository import DeliveryRepository, InMemoryDeliveryRepository

COURIERS = ("M01", "M02", "M03")  # 假的外送員名單：D001 給 M01、D002 給 M02……輪流


class Dispatch:
    def __init__(self, deliveries: DeliveryRepository | None = None, *, couriers: Sequence[str] = COURIERS) -> None:
        self._deliveries = deliveries or InMemoryDeliveryRepository()
        self._couriers = tuple(couriers)

    def all(self) -> list[Delivery]:
        return self._deliveries.all()

    def by_order(self, order_id: str) -> Delivery | None:
        return self._deliveries.by_order(order_id)

    # 訂閱 DeliveryRequested：開一張配送單，等餐廳接單
    def on_delivery_requested(self, event: contracts.DeliveryRequested) -> None:
        if self._deliveries.seen(event.event_id):
            print(f"外送：這則 DeliveryRequested 處理過了（訂單 {event.order_id}），不再開單", flush=True)
        else:
            delivery = Delivery(self._deliveries.next_id(), event.order_id, event.restaurant_id, event.delivery_address)
            if self._deliveries.add(delivery, message_id=event.event_id):
                print(
                    f"外送：開配送單 {delivery.delivery_id}（訂單 {event.order_id}）{delivery.pickup} → {delivery.dropoff}",
                    flush=True,
                )
        self._advance(event.order_id)

    # 訂閱 TicketAccepted：餐廳接單了。先記下來，再看能不能指派
    def on_ticket_accepted(self, event: contracts.TicketAccepted) -> None:
        if not self._deliveries.note_ticket(event.order_id, event.ticket_id, message_id=event.event_id):
            print(f"外送：這則 TicketAccepted 處理過了（訂單 {event.order_id}）", flush=True)
        self._advance(event.order_id)

    def _advance(self, order_id: str) -> None:
        """配送單和「餐廳接單了」都到了，才指派外送員。看狀態決定，重做幾次結果都一樣。"""
        delivery = self._deliveries.by_order(order_id)
        ticket_id = self._deliveries.accepted_ticket(order_id)
        if delivery is None or delivery.status is not DeliveryStatus.WAITING:
            return
        if ticket_id is None:
            print(f"外送：{delivery.delivery_id} 等餐廳接單（訂單 {order_id}）", flush=True)
            return
        number = int(delivery.delivery_id[1:])
        delivery.assign(self._couriers[(number - 1) % len(self._couriers)])
        message = contracts.CourierAssigned(
            order_id=order_id, delivery_id=delivery.delivery_id, courier_id=delivery.courier_id
        )
        self._deliveries.save(delivery, announce=[message])
        print(
            f"外送：{order_id} 餐廳已接單（{ticket_id}）→ {delivery.delivery_id} 指派外送員 {delivery.courier_id}，"
            "公告 CourierAssigned",
            flush=True,
        )
