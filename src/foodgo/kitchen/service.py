"""廚房的應用服務：開廚房單、接單或拒單、出餐。

第 7 集（編舞式）：聽 OrderPlaced 開單、聽付款的事件決定接不接。
第 8 集（編排式）：收到編排器的命令 AcceptTicket／CancelTicket 才動，做完回覆一則事件。
"""
import time
from collections.abc import Callable, Iterable

from foodgo import contracts
from foodgo.kitchen.repository import InMemoryTicketRepository, TicketRepository
from foodgo.kitchen.ticket import Dish, Ticket, TicketStatus


BUSY = "餐廳太忙，暫停接單"  # FOODGO_FAULT_KITCHEN=reject 時拒單的理由


class KitchenDown(Exception):
    """廚房系統掛了。用環境變數 FOODGO_FAULT_KITCHEN=crash 模擬。"""


class Kitchen:
    def __init__(
        self,
        publish: Callable[[contracts.IntegrationEvent], None] | None = None,
        *,
        fault: str = "",
        tickets: TicketRepository | None = None,
        slow_seconds: float = 4.0,
    ) -> None:
        self._publish = publish or (lambda message: None)  # 第 3 集的行程內匯流排；Kafka 那條路走 outbox
        self._fault = fault  # crash：開不了單；reject：付款過了也拒單（第 7 集）；slow：慢吞吞才回（第 8 集）
        self._tickets = tickets or InMemoryTicketRepository()
        self._slow_seconds = slow_seconds  # slow 時每一則命令要想多久（比編排器的逾時長一點）

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
        message = self._decide(ticket)
        self._tickets.save(ticket, announce=[message])
        self._publish(message)
        print(
            f"廚房：{order_id} 付款已授權 → {ticket.ticket_id} {ticket.status.value}，"
            f"公告 {type(message).__name__}",
            flush=True,
        )

    def _decide(self, ticket: Ticket) -> contracts.IntegrationEvent:
        """接單或拒單（FOODGO_FAULT_KITCHEN=reject：付款過了也拒單）。回傳要公告的事件。"""
        if self._fault == "reject":
            ticket.reject()
            return contracts.TicketRejected(order_id=ticket.order_id, ticket_id=ticket.ticket_id, reason=BUSY)
        ticket.accept()
        return contracts.TicketAccepted(order_id=ticket.order_id, ticket_id=ticket.ticket_id)

    # ------------------------------------------------------------ 第 8 集：編排式，聽命令、回覆結果
    def on_accept_ticket(self, command: contracts.AcceptTicket) -> None:
        """付款過了，編排器要廚房接單：開單、決定接不接、回覆，同一個交易。

        同一張訂單已經有廚房單了（編排器逾時重送）：照它現在的樣子再回一次，不開第二張。
        """
        if self._fault == "crash":
            raise KitchenDown("廚房系統當機，收不了單")
        if self._fault == "slow":
            print(f"廚房：好忙，{self._slow_seconds:g} 秒後才回（訂單 {command.order_id}）", flush=True)
            time.sleep(self._slow_seconds)
        ticket = self._tickets.by_order(command.order_id)
        if ticket is not None:
            reply = self._decide(ticket) if ticket.status is TicketStatus.RECEIVED else self._answer(ticket)
            self._tickets.save(ticket, announce=[reply])
            print(f"廚房：{ticket.ticket_id} 已經是 {ticket.status.value}，再回一次 {type(reply).__name__}", flush=True)
            return
        dishes = tuple(Dish(item.name, item.quantity) for item in command.items)
        ticket = Ticket(self._tickets.next_id(), command.order_id, command.restaurant_id, dishes, command.note)
        reply = self._decide(ticket)
        if not self._tickets.add(ticket, message_id=command.event_id, announce=[reply]):
            print(f"廚房：這則 AcceptTicket 處理過了（訂單 {command.order_id}）", flush=True)
            return
        menu = "、".join(f"{d.name} ×{d.quantity}" for d in ticket.dishes)
        print(
            f"廚房：開廚房單 {ticket.ticket_id}（訂單 {ticket.order_id}）{menu}；備註：{ticket.note or '無'}"
            f" → {ticket.status.value}，回覆 {type(reply).__name__}",
            flush=True,
        )
        self._publish(reply)

    def on_cancel_ticket(self, command: contracts.CancelTicket) -> None:
        """補償：取消廚房單。已經出餐的取消不了（回不去的那一步），不回覆，讓編排器自己逾時。"""
        ticket = self._tickets.by_order(command.order_id)
        if ticket is None:
            print(f"廚房：沒有訂單 {command.order_id} 的廚房單，不用取消", flush=True)
            return
        if ticket.status is TicketStatus.READY:
            print(f"廚房：{ticket.ticket_id} 已經出餐了，取消不了（訂單 {command.order_id}）", flush=True)
            return
        if ticket.status in (TicketStatus.RECEIVED, TicketStatus.ACCEPTED):
            ticket.cancel()
        reply = contracts.TicketCancelled(order_id=ticket.order_id, ticket_id=ticket.ticket_id, reason=command.reason)
        self._tickets.save(ticket, announce=[reply])
        print(f"廚房：{ticket.ticket_id} → {ticket.status.value}（{command.reason}），回覆 TicketCancelled", flush=True)

    @staticmethod
    def _answer(ticket: Ticket) -> contracts.IntegrationEvent:
        """已經決定過的廚房單，用共同語言再說一次。"""
        if ticket.status in (TicketStatus.ACCEPTED, TicketStatus.READY):
            return contracts.TicketAccepted(order_id=ticket.order_id, ticket_id=ticket.ticket_id)
        reason = BUSY if ticket.status is TicketStatus.REJECTED else "這張單已經取消了"
        return contracts.TicketRejected(order_id=ticket.order_id, ticket_id=ticket.ticket_id, reason=reason)

    # 出餐：廚房單標成備妥，對外公告 MealReady（誰要聽，廚房不用知道）
    def mark_ready(self, ticket: Ticket) -> None:
        ticket.mark_ready()
        message = contracts.MealReady(
            order_id=ticket.order_id, ticket_id=ticket.ticket_id, restaurant_id=ticket.restaurant_id
        )
        self._tickets.save(ticket, announce=[message])
        print(f"廚房：{ticket.ticket_id} 出餐，發布 MealReady {ticket.order_id}", flush=True)
        self._publish(message)
