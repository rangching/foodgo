"""外送的應用服務（派車台）：開配送單、指派外送員。

第 7 集（編舞式）：先開配送單等著，聽到餐廳接單了才指派。
第 8 集（編排式）：收到編排器的命令 AssignCourier，開單＋指派＋回覆一次做完。
"""
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

    # ------------------------------------------------------------ 第 8 集：編排式，聽命令、回覆結果
    def on_assign_courier(self, command: contracts.AssignCourier) -> None:
        """餐廳接單了，編排器要外送派人。已經派過了（編排器重送）：再回一次同一個外送員。"""
        delivery = self._deliveries.by_order(command.order_id)
        if delivery is not None and delivery.status is DeliveryStatus.ASSIGNED:
            self._deliveries.save(delivery, announce=[self._assigned(delivery)])
            print(f"外送：{delivery.delivery_id} 已經指派給 {delivery.courier_id}，再回一次 CourierAssigned", flush=True)
            return
        if delivery is None:
            delivery = Delivery(
                self._deliveries.next_id(), command.order_id, command.restaurant_id, command.delivery_address
            )
            self._pick_courier(delivery)
            if not self._deliveries.add(delivery, message_id=command.event_id, announce=[self._assigned(delivery)]):
                print(f"外送：這則 AssignCourier 處理過了（訂單 {command.order_id}）", flush=True)
                return
        else:  # 第 7 集留下來、還在等的配送單
            self._pick_courier(delivery)
            self._deliveries.save(delivery, announce=[self._assigned(delivery)])
        print(
            f"外送：開配送單 {delivery.delivery_id}（訂單 {delivery.order_id}）{delivery.pickup} → {delivery.dropoff}，"
            f"指派外送員 {delivery.courier_id}，回覆 CourierAssigned",
            flush=True,
        )

    def _pick_courier(self, delivery: Delivery) -> None:
        number = int(delivery.delivery_id[1:])
        delivery.assign(self._couriers[(number - 1) % len(self._couriers)])  # D001 給 M01、D002 給 M02……輪流

    @staticmethod
    def _assigned(delivery: Delivery) -> contracts.CourierAssigned:
        return contracts.CourierAssigned(
            order_id=delivery.order_id, delivery_id=delivery.delivery_id, courier_id=delivery.courier_id
        )

    # ------------------------------------------------------------ 第 7 集：編舞式
    def _advance(self, order_id: str) -> None:
        """配送單和「餐廳接單了」都到了，才指派外送員。看狀態決定，重做幾次結果都一樣。"""
        delivery = self._deliveries.by_order(order_id)
        ticket_id = self._deliveries.accepted_ticket(order_id)
        if delivery is None or delivery.status is not DeliveryStatus.WAITING:
            return
        if ticket_id is None:
            print(f"外送：{delivery.delivery_id} 等餐廳接單（訂單 {order_id}）", flush=True)
            return
        self._pick_courier(delivery)
        self._deliveries.save(delivery, announce=[self._assigned(delivery)])
        print(
            f"外送：{order_id} 餐廳已接單（{ticket_id}）→ {delivery.delivery_id} 指派外送員 {delivery.courier_id}，"
            "公告 CourierAssigned",
            flush=True,
        )
