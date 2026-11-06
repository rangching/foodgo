"""廚房的應用服務：開廚房單、接單或拒單、出餐。"""
from collections.abc import Callable, Iterable

from foodgo import contracts
from foodgo.kitchen.repository import InMemoryTicketRepository, TicketRepository
from foodgo.kitchen.ticket import Dish, Ticket, TicketStatus


class KitchenDown(Exception):
    """廚房系統掛了。用環境變數 FOODGO_FAULT_KITCHEN=crash 模擬。"""


class Kitchen:
    def __init__(
        self,
        publish: Callable[[contracts.IntegrationEvent], None] | None = None,
        *,
        fault: str = "",
        tickets: TicketRepository | None = None,
    ) -> None:
        self._publish = publish or (lambda message: None)  # 第 3 集的行程內匯流排；Kafka 那條路走 outbox
        self._fault = fault  # crash：開不了單；reject：付款過了也拒單（第 7 集）
        self._tickets = tickets or InMemoryTicketRepository()

    def tickets(self) -> list[Ticket]:
        return self._tickets.all()

    def get(self, ticket_id: str) -> Ticket | None:
        return self._tickets.get(ticket_id)

    def create_ticket(
        self,
        order_id: str,
        restaurant_id: str,
        dishes: Iterable[Dish],
        note: str = "",
        *,
        message_id: str | None = None,
    ) -> Ticket | None:
        if self._fault == "crash":
            raise KitchenDown("廚房系統當機，收不了單")
        ticket = Ticket(self._tickets.next_id(), order_id, restaurant_id, tuple(dishes), note)
        if not self._tickets.add(ticket, message_id=message_id):
            print(f"廚房：這則訊息剛剛已經開過單了（訂單 {order_id}），不再開單", flush=True)
            return None
        menu = "、".join(f"{d.name} ×{d.quantity}" for d in ticket.dishes)
        print(f"廚房：開廚房單 {ticket.ticket_id}（訂單 {order_id}）{menu}；備註：{note or '無'}", flush=True)
        return ticket

    # 訂閱 OrderPlaced：收到「訂單已送出」就開一張廚房單。同一則訊息收到兩次，只開一張
    def on_order_placed(self, event: contracts.OrderPlaced) -> None:
        if self._tickets.seen(event.event_id):
            print(f"廚房：這則 OrderPlaced 處理過了（訂單 {event.order_id}，event_id {event.event_id[:8]}…），不再開單", flush=True)
        else:
            dishes = [Dish(item.name, item.quantity) for item in event.items]
            self.create_ticket(event.order_id, event.restaurant_id, dishes, event.note, message_id=event.event_id)
        self._advance(event.order_id)  # 付款的結果可能早就到了

    # ------------------------------------------------------------ 第 7 集：編舞式的一棒
    # 訂閱 PaymentAuthorized／PaymentFailed：先記下聽到什麼，再看能不能往下走
    def on_payment_authorized(self, event: contracts.PaymentAuthorized) -> None:
        if not self._tickets.note_payment(event.order_id, "authorized", message_id=event.event_id):
            print(f"廚房：這則 PaymentAuthorized 處理過了（訂單 {event.order_id}）", flush=True)
        self._advance(event.order_id)

    def on_payment_failed(self, event: contracts.PaymentFailed) -> None:
        if not self._tickets.note_payment(event.order_id, "failed", event.reason, message_id=event.event_id):
            print(f"廚房：這則 PaymentFailed 處理過了（訂單 {event.order_id}）", flush=True)
        self._advance(event.order_id)

    def _advance(self, order_id: str) -> None:
        """廚房單和付款結果都到了，才決定接單、拒單或不做。看狀態決定，重做幾次結果都一樣（冪等）。"""
        ticket = self._tickets.by_order(order_id)
        payment = self._tickets.payment_of(order_id)
        if ticket is None or ticket.status is not TicketStatus.RECEIVED:
            return  # 還沒收到 OrderPlaced，或是已經決定過了
        if payment is None:
            print(f"廚房：{ticket.ticket_id} 等付款（訂單 {order_id}）", flush=True)
            return
        outcome, reason = payment
        if outcome == "failed":
            ticket.cancel()
            self._tickets.save(ticket)
            print(f"廚房：{order_id} 付款失敗（{reason}）→ {ticket.ticket_id} 不做了", flush=True)
            return
        if self._fault == "reject":
            ticket.reject()
            message = contracts.TicketRejected(
                order_id=order_id, ticket_id=ticket.ticket_id, reason="餐廳太忙，暫停接單"
            )
        else:
            ticket.accept()
            message = contracts.TicketAccepted(order_id=order_id, ticket_id=ticket.ticket_id)
        self._tickets.save(ticket, announce=[message])
        self._publish(message)
        print(
            f"廚房：{order_id} 付款已授權 → {ticket.ticket_id} {ticket.status.value}，"
            f"公告 {type(message).__name__}",
            flush=True,
        )

    # 出餐：廚房單標成備妥，對外公告 MealReady（誰要聽，廚房不用知道）
    def mark_ready(self, ticket: Ticket) -> None:
        ticket.mark_ready()
        message = contracts.MealReady(
            order_id=ticket.order_id, ticket_id=ticket.ticket_id, restaurant_id=ticket.restaurant_id
        )
        self._tickets.save(ticket, announce=[message])
        print(f"廚房：{ticket.ticket_id} 出餐，發布 MealReady {ticket.order_id}", flush=True)
        self._publish(message)
