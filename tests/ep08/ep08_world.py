"""第 8 集的測試工具：A001 的三則事實、假的時鐘、記憶體版的「Kafka」（命令交給參與者、回覆交回編排器）。

測試檔用 from ep08_world import …（pytest 會把這個資料夾放進 sys.path）。
"""
from datetime import UTC, datetime, timedelta

START = datetime(2026, 11, 6, 6, 50, tzinfo=UTC)  # 臺北時間 14:50


def a001():
    from foodgo.ordering.model import Address, Money, Order

    order = Order(
        order_id="A001",
        customer_id="C042",
        restaurant_id="R007",
        delivery_address=Address("信義路五段 7 號"),
        delivery_fee=Money(30),
        note="不要香菜",
    )
    order.add_line("牛肉麵", Money(160))
    order.add_line("紅茶", Money(40))
    order.place()
    return order


def facts():
    """A001 送出時點餐公告的三則：OrderPlaced、PaymentRequested、DeliveryRequested。"""
    from foodgo.ordering.integration import announcements

    order = a001()
    return announcements(order, order.pull_events())


class Clock:
    """假的時鐘：測試自己撥，不用真的等 3 秒。"""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Orders:
    """記憶體版的點餐 Repository，存檔時照第 8 集的規則公告（announcements），公告的放在 announced。"""

    def __init__(self) -> None:
        self._orders = {}
        self.announced = []

    def next_id(self) -> str:
        return f"A{len(self._orders) + 1:03d}"

    def get(self, order_id):
        return self._orders.get(order_id)

    def save(self, order) -> None:
        from foodgo.ordering.integration import announcements

        self._orders[order.order_id] = order
        self.announced.extend(announcements(order, order.pull_events()))


class World:
    """記憶體版的整條流程：編排器＋點餐、付款、廚房、外送四個參與者，中間用 list 當 Kafka。

    down：沒在跑的參與者（命令先排著，revive() 之後照順序處理）；slow：參與者要幾秒才處理一則命令。
    """

    def __init__(
        self,
        *,
        payment_fault: str = "",
        refund_fault: str = "",
        kitchen_fault: str = "",
        down: tuple[str, ...] = (),
        slow: dict[str, float] | None = None,
        timeout: float = 3,
        retries: int = 1,
    ) -> None:
        from foodgo.delivery.repository import InMemoryDeliveryRepository
        from foodgo.delivery.service import Dispatch
        from foodgo.kitchen.repository import InMemoryTicketRepository
        from foodgo.kitchen.service import Kitchen
        from foodgo.payment.fakepay import FakePay
        from foodgo.payment.gateway import FakePayAdapter
        from foodgo.payment.repository import InMemoryPaymentRepository
        from foodgo.payment.service import Payments
        from foodgo.saga.machine import Settings
        from foodgo.saga.orchestrator import Orchestrator
        from foodgo.saga.repository import InMemorySagaRepository

        self.clock = Clock()
        self.sagas = InMemorySagaRepository()
        self.orchestrator = Orchestrator(self.sagas, Settings(timeout=timeout, retries=retries), clock=self.clock)
        self.orders = Orders()
        self.payments = InMemoryPaymentRepository()
        self.tickets = InMemoryTicketRepository()
        self.deliveries = InMemoryDeliveryRepository()
        self.payment = Payments(FakePayAdapter(FakePay(fault=payment_fault, refund_fault=refund_fault)), self.payments)
        self.kitchen = Kitchen(tickets=self.tickets, fault=kitchen_fault)
        self.dispatch = Dispatch(self.deliveries)
        self.down = set(down)
        self.slow = slow or {}
        self.queue: list[tuple[datetime, str, object]] = []  # (幾點可以處理, 誰, 命令)
        self.handled: list[str] = []  # 參與者處理過的命令名稱，照順序
        self._sent = 0
        self._heard = {"ordering": 0, "payment": 0, "kitchen": 0, "delivery": 0}

    # ------------------------------------------------------------ 下單、跑、等
    def place(self) -> None:
        order = a001()
        self.orders.save(order)  # 三則事實寫進「ordering.events」
        self.pump()

    def pump(self) -> None:
        """把能送的都送完：命令 → 參與者，回覆 → 編排器，直到沒有新的訊息。"""
        while True:
            moved = self._hear_replies()
            for when, who, command in list(self.queue):
                if who not in self.down and when <= self.clock.now:
                    self.queue.remove((when, who, command))
                    self._handle(who, command)
                    moved = True
            for _, command in self.sagas.sent[self._sent :]:
                self._sent += 1
                delay = self.slow.get(command.receiver, 0)
                self.queue.append((self.clock.now + timedelta(seconds=delay), command.receiver, command))
                moved = True
            if not moved:
                return

    def wait(self, seconds: float) -> None:
        """時間過去 seconds 秒：每過一秒看一次逾時（和 consumer 閒下來時一樣）。"""
        for _ in range(int(seconds)):
            self.clock.advance(1)
            self.orchestrator.tick()
            self.pump()

    def revive(self, who: str) -> None:
        """參與者修好重啟：排著的命令照順序處理。"""
        self.down.discard(who)
        if who == "kitchen":
            from foodgo.kitchen.service import Kitchen

            self.kitchen = Kitchen(tickets=self.tickets)
        self.pump()

    # ------------------------------------------------------------ 看結果
    @property
    def saga(self):
        return self.sagas.get("A001")

    def log(self) -> list[tuple[str, str, str]]:
        return [(e.state.value, e.action, e.message) for e in self.sagas.log("A001")]

    # ------------------------------------------------------------ 內部
    def _handle(self, who: str, command) -> None:
        from foodgo.ordering.consumer import on_cancel_order

        self.handled.append(type(command).__name__)
        name = type(command).__name__
        if who == "ordering":
            on_cancel_order(self.orders, command)
        elif who == "payment":
            {"AuthorizePayment": self.payment.on_authorize_payment, "RefundPayment": self.payment.on_refund_payment}[
                name
            ](command)
        elif who == "kitchen":
            {"AcceptTicket": self.kitchen.on_accept_ticket, "CancelTicket": self.kitchen.on_cancel_ticket}[name](command)
        elif who == "delivery":
            self.dispatch.on_assign_courier(command)

    def _hear_replies(self) -> bool:
        moved = False
        sources = {
            "ordering": self.orders.announced,
            "payment": self.payments.announced,
            "kitchen": self.tickets.announced,
            "delivery": self.deliveries.announced,
        }
        for who, messages in sources.items():
            for message in messages[self._heard[who] :]:
                self._heard[who] += 1
                self.orchestrator.on_event(type(message).from_json(message.to_json()))  # 走一趟 JSON，像真的 Kafka
                moved = True
        return moved


