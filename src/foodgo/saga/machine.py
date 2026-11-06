"""OrderSaga 的狀態機：記住一張訂單走到哪、下一步叫誰做、失敗時倒回去補償哪幾步。

不碰資料庫、不碰 Kafka、不看時鐘（現在幾點由外面給）：不開 Docker 就能測。

  started ─收齊點餐的三則事實→ payment_pending ─PaymentAuthorized→ kitchen_pending ─TicketAccepted→ delivery_pending ─CourierAssigned→ completed
                                  │PaymentFailed                    │TicketRejected／逾時重試仍沒回覆
                                  └──────────────→ compensating ←───┘
                                                    │ 每一則補償都回覆了 → cancelled
                                                    └ 補償失敗、重試了還是失敗 → needs_attention（人工處理）

補償的順序和正常流程相反：後做的先補（退款 → 取消訂單）。
每一步送出命令就開始計時；逾時重送一次（Settings.retries），還是沒回覆就放棄這一步、開始補償。
放棄的那一步「結果不確定」（也許對方其實做了，只是很慢）：它的補償照送，但不等回覆——
命令 topic 照順序，補償一定排在原本的命令後面，對方回來之後會先做、再撤銷。
"""
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from foodgo import contracts


class SagaState(StrEnum):
    STARTED = "started"  # 收到訂單了，還在收齊點餐給的資料
    PAYMENT_PENDING = "payment_pending"  # 已送 AuthorizePayment，等付款回覆
    KITCHEN_PENDING = "kitchen_pending"  # 已送 AcceptTicket，等廚房回覆
    DELIVERY_PENDING = "delivery_pending"  # 已送 AssignCourier，等外送回覆
    COMPLETED = "completed"  # 外送員指派好了：整條流程走完
    COMPENSATING = "compensating"  # 倒回去中：一則一則送補償命令
    CANCELLED = "cancelled"  # 補償做完，訂單已取消
    NEEDS_ATTENTION = "needs_attention"  # 補償重試了還是失敗：要人工處理


FINISHED = frozenset({SagaState.COMPLETED, SagaState.CANCELLED, SagaState.NEEDS_ATTENTION})


@dataclass(frozen=True)
class Step:
    """一步：送哪個命令、等哪個回覆。"""

    command: str  # 送出的命令
    done: str  # 成功的回覆
    failed: str = ""  # 失敗的回覆（沒有就是空字串）
    undo: str = ""  # 這一步做成了、後面卻失敗時，要送的補償命令
    pending: SagaState = SagaState.COMPENSATING  # 等回覆時的狀態
    who: str = ""  # 誰收這個命令（印紀錄用）


# 正常流程：照順序一步一步走
FORWARD = (
    Step("AuthorizePayment", "PaymentAuthorized", "PaymentFailed", "RefundPayment", SagaState.PAYMENT_PENDING, "付款"),
    Step("AcceptTicket", "TicketAccepted", "TicketRejected", "CancelTicket", SagaState.KITCHEN_PENDING, "廚房"),
    Step("AssignCourier", "CourierAssigned", pending=SagaState.DELIVERY_PENDING, who="外送"),
)
# 補償：倒回去時送的命令
COMPENSATIONS = (
    Step("RefundPayment", "PaymentRefunded", "RefundFailed", who="付款"),
    Step("CancelTicket", "TicketCancelled", who="廚房"),
    Step("CancelOrder", "OrderCancelled", who="點餐"),
)
STEPS = {step.command: step for step in FORWARD + COMPENSATIONS}

# 外送那一步不計時：派車台一定派得到人（輪流指派）。練習：15 分鐘派不到人就取消並退款
NO_DEADLINE = frozenset({"AssignCourier"})

# 點餐送出時公告的三則事實：編排器收齊了才出發（同一個交易寫進 outbox、同一個 key，照順序到）
FACTS = ("OrderPlaced", "PaymentRequested", "DeliveryRequested")
NEEDED = ("items", "amount", "delivery_address")  # 三則各帶來的、出發前一定要有的資料


@dataclass(frozen=True)
class Settings:
    """等多久、重試幾次。影片示範用很短的秒數；正式上線會是幾十秒到幾分鐘。"""

    timeout: float = 3.0  # 秒：送出命令後等回覆多久
    retries: int = 1  # 逾時（或退款失敗）之後再送幾次

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            timeout=float(os.environ.get("FOODGO_SAGA_TIMEOUT", "3")),
            retries=int(os.environ.get("FOODGO_SAGA_RETRIES", "1")),
        )


@dataclass(frozen=True)
class Entry:
    """saga_log 的一列：做了什麼、之後是什麼狀態。"""

    state: SagaState  # 寫這一列時 saga 的狀態（收到：還沒轉移；送出、結束：已經轉移）
    action: str  # receive／send／timeout／retry／compensate／finish／give_up／ignore
    message: str  # 收到或送出的訊息名稱，例如 AuthorizePayment
    detail: str = ""  # 給人看的一句話


@dataclass(eq=False)
class OrderSaga:
    """一張訂單一個 saga（saga_id＝order_id）。像聚合一樣：方法改狀態，要送的命令、要記的 log 先記著，由外面一次取走。"""

    saga_id: str
    state: SagaState = SagaState.STARTED
    data: dict[str, Any] = field(default_factory=dict)  # 點餐給的事實與一路收到的編號（payment_id、ticket_id…）
    waiting_for: str = ""  # 正在等哪個命令的回覆；空字串＝沒在等
    attempts: int = 0  # 這個命令送了幾次
    deadline: datetime | None = None  # 等到什麼時候；None＝不限時
    done: list[str] = field(default_factory=list)  # 做成的正向步驟（命令名稱），補償時倒著看
    todo: list[str] = field(default_factory=list)  # 還沒送的補償命令，照順序
    reason: str = ""  # 為什麼要倒回去
    version: int = 0  # 存了幾次（樂觀鎖用）
    _sent: list[contracts.Command] = field(default_factory=list, repr=False)
    _log: list[Entry] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------ 外面呼叫的三個入口
    def receive(self, message: contracts.IntegrationEvent, now: datetime, settings: Settings) -> None:
        """收到一則事件：點餐的事實，或參與者的回覆。"""
        kind = type(message).__name__
        if kind in FACTS:
            self._note_fact(message, now, settings)
            return
        step = STEPS.get(self.waiting_for)
        if step is not None and kind == step.done:
            self._succeeded(step, message, now, settings)
        elif step is not None and kind == step.failed:
            self._failed(step, message, now, settings)
        elif kind == "OrderCancelled" and self.state not in FINISHED | {SagaState.COMPENSATING}:
            self._write("receive", kind, f"顧客自己取消了：{message.reason}")  # 不是編排器叫的
            self._compensate(f"顧客取消：{message.reason}", now, settings, uncertain=step, order_cancelled=True)
        elif kind == "RefundFailed" and self.state is not SagaState.NEEDS_ATTENTION:
            self._write("receive", kind, message.reason)  # 不等回覆的那一則退款，後來失敗了
            self._give_up(f"退款失敗（{message.reason}），錢還沒退")
        else:
            self._write("ignore", kind, f"{describe(message)}：不是現在在等的回覆（重複或遲到），略過")

    def check_deadline(self, now: datetime, settings: Settings) -> None:
        """時間到了還沒等到回覆：重送，或放棄這一步。"""
        if self.deadline is None or now < self.deadline or not self.waiting_for:
            return
        step = STEPS[self.waiting_for]
        self._write("timeout", step.command, f"{settings.timeout:g} 秒沒有回覆（第 {self.attempts} 次）")
        if self.attempts <= settings.retries:
            self._resend(now, settings)
        elif step in FORWARD:
            waited = f"{step.who}沒有回覆：送了 {self.attempts} 次，各等 {settings.timeout:g} 秒"
            self._compensate(waited, now, settings, uncertain=step)
        else:
            self._give_up(f"{step.command} 送了 {self.attempts} 次都沒有回覆")

    @property
    def written(self) -> tuple[Entry, ...]:
        """這次改動要記的 log（還沒存）：印紀錄用。"""
        return tuple(self._log)

    def pull(self) -> tuple[list[contracts.Command], list[Entry]]:
        """取走要送的命令與要記的 log（Repository 存檔時呼叫：和 saga 的新狀態同一個交易）。"""
        sent, log = self._sent, self._log
        self._sent, self._log = [], []
        return sent, log

    # ------------------------------------------------------------ 出發前：收齊點餐的事實
    def _note_fact(self, message: contracts.IntegrationEvent, now: datetime, settings: Settings) -> None:
        if isinstance(message, contracts.OrderPlaced):
            self.data.update(
                restaurant_id=message.restaurant_id,
                items=[{"name": i.name, "quantity": i.quantity} for i in message.items],
                note=message.note,
            )
        elif isinstance(message, contracts.PaymentRequested):
            self.data.update(customer_id=message.customer_id, amount=message.amount)
        elif isinstance(message, contracts.DeliveryRequested):
            self.data.update(restaurant_id=message.restaurant_id, delivery_address=message.delivery_address)
        self._write("receive", type(message).__name__, describe(message))
        if self.state is SagaState.STARTED and all(key in self.data for key in NEEDED):
            self._forward(FORWARD[0], now, settings)

    # ------------------------------------------------------------ 正常流程
    def _forward(self, step: Step, now: datetime, settings: Settings) -> None:
        self.state = step.pending
        self._send(step.command, now, settings, action="send")

    def _succeeded(self, step: Step, message: contracts.IntegrationEvent, now: datetime, settings: Settings) -> None:
        for key in ("payment_id", "ticket_id", "delivery_id", "courier_id"):
            if getattr(message, key, ""):
                self.data[key] = getattr(message, key)
        self._stop_waiting()
        if step in FORWARD:
            self.done.append(step.command)
            self._write("receive", type(message).__name__, describe(message))
            following = FORWARD.index(step) + 1
            if following == len(FORWARD):
                self.state = SagaState.COMPLETED
                self._write("finish", "", "整條流程走完")
                return
            self._forward(FORWARD[following], now, settings)
            return
        self._write("receive", type(message).__name__, describe(message))  # 一則補償做完了
        self._next_compensation(now, settings)

    def _failed(self, step: Step, message: contracts.IntegrationEvent, now: datetime, settings: Settings) -> None:
        reason = getattr(message, "reason", "")
        if step in FORWARD:  # 對方說不：這一步沒做成，前面做成的倒回去
            self._stop_waiting()
            self._write("receive", type(message).__name__, reason)
            label = {"AuthorizePayment": "付款失敗", "AcceptTicket": "餐廳拒單"}.get(step.command, f"{step.who}失敗")
            self._compensate(f"{label}：{reason}", now, settings, uncertain=None)
            return
        self._write("receive", type(message).__name__, reason)  # 補償本身失敗了
        if self.attempts <= settings.retries:
            self._resend(now, settings)
        else:
            self._give_up(f"{step.command} 試了 {self.attempts} 次都失敗（{reason}）")

    # ------------------------------------------------------------ 倒回去
    def _compensate(
        self,
        reason: str,
        now: datetime,
        settings: Settings,
        *,
        uncertain: Step | None,
        order_cancelled: bool = False,
    ) -> None:
        """開始補償：做成的步驟倒著抵銷，最後取消訂單。uncertain＝放棄等待、結果不確定的那一步。"""
        self.state = SagaState.COMPENSATING
        self.reason = reason
        self._stop_waiting()
        if uncertain is not None and uncertain.undo:
            self._sent.append(self._command(uncertain.undo))
            self._write("compensate", uncertain.undo, f"{uncertain.who}有沒有做不確定：先送補償、不等回覆")
        self.todo = [STEPS[command].undo for command in reversed(self.done) if STEPS[command].undo]
        if not order_cancelled:
            self.todo.append("CancelOrder")
        self._next_compensation(now, settings)

    def _next_compensation(self, now: datetime, settings: Settings) -> None:
        if not self.todo:
            self.state = SagaState.CANCELLED
            self._write("finish", "", "補償做完，訂單已取消")
            return
        self._send(self.todo.pop(0), now, settings, action="compensate")

    def _give_up(self, why: str) -> None:
        self.state = SagaState.NEEDS_ATTENTION
        self._stop_waiting()
        self._write("give_up", "", f"要人工處理：{why}")

    # ------------------------------------------------------------ 送命令、計時
    def _send(self, command: str, now: datetime, settings: Settings, *, action: str) -> None:
        message = self._command(command)
        self._sent.append(message)
        self.waiting_for = command
        self.attempts = 1
        self.deadline = None if command in NO_DEADLINE else now + timedelta(seconds=settings.timeout)
        self._write(action, command, describe(message))

    def _resend(self, now: datetime, settings: Settings) -> None:
        """同一個命令再送一次（新的編號：參與者照訂單編號認，重做也不會做兩次）。"""
        message = self._command(self.waiting_for)
        self._sent.append(message)
        self.attempts += 1
        self.deadline = now + timedelta(seconds=settings.timeout)
        self._write("retry", self.waiting_for, f"再送一次（第 {self.attempts} 次）")

    def _stop_waiting(self) -> None:
        self.waiting_for, self.attempts, self.deadline = "", 0, None

    def _command(self, name: str) -> contracts.Command:
        """照 saga 記住的資料做出命令。每次都是新的一則（新的 event_id）。"""
        d, order_id = self.data, self.saga_id
        items = tuple(contracts.OrderItem(name=i["name"], quantity=i["quantity"]) for i in d.get("items", []))
        build = {
            "AuthorizePayment": lambda: contracts.AuthorizePayment(
                order_id=order_id, customer_id=d["customer_id"], amount=d["amount"]
            ),
            "AcceptTicket": lambda: contracts.AcceptTicket(
                order_id=order_id, restaurant_id=d["restaurant_id"], items=items, note=d.get("note", "")
            ),
            "AssignCourier": lambda: contracts.AssignCourier(
                order_id=order_id, restaurant_id=d["restaurant_id"], delivery_address=d["delivery_address"]
            ),
            "RefundPayment": lambda: contracts.RefundPayment(order_id=order_id, reason=self.reason),
            "CancelTicket": lambda: contracts.CancelTicket(order_id=order_id, reason=self.reason),
            "CancelOrder": lambda: contracts.CancelOrder(order_id=order_id, reason=self.reason),
        }
        return build[name]()

    def _write(self, action: str, message: str, detail: str = "") -> None:
        self._log.append(Entry(self.state, action, message, detail))


def describe(message: contracts.Message) -> str:
    """一則訊息用一句話說（saga_log 的 detail）。"""
    match message:
        case contracts.OrderPlaced() | contracts.AcceptTicket():
            menu = "、".join(f"{i.name} ×{i.quantity}" for i in message.items)
            return f"{menu}；備註：{message.note or '無'}"
        case contracts.PaymentRequested() | contracts.AuthorizePayment():
            return f"{message.customer_id} 付 {message.amount} 元"
        case contracts.DeliveryRequested() | contracts.AssignCourier():
            return f"{message.restaurant_id} → {message.delivery_address}"
        case contracts.PaymentAuthorized():
            return message.payment_id
        case contracts.PaymentRefunded():
            return f"{message.payment_id or '沒有付款'} 退 {message.amount} 元"
        case contracts.TicketAccepted():
            return message.ticket_id
        case contracts.CourierAssigned():
            return f"{message.delivery_id}，外送員 {message.courier_id}"
        case contracts.TicketCancelled():
            return f"{message.ticket_id} 已取消"
        case contracts.OrderCancelled():
            return message.reason
        case contracts.RefundPayment() | contracts.CancelTicket() | contracts.CancelOrder():
            return message.reason
        case _:
            return getattr(message, "reason", "")
