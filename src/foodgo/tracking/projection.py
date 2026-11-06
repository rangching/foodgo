"""訂單追蹤的投影：一則事件進來，追蹤表的那一列變成什麼樣子。純函式：不碰資料庫、不碰 Kafka、不看時鐘。

  apply(追蹤 | None, 事件) → 追蹤          project(事件們) → 追蹤（一則一則 apply）

兩個性質讓它可以放心重播：
  冪等：同一則事件套兩次，和套一次一樣（Kafka 至少送一次；參與者被重送命令時也會再回一次）。
  不管順序：五條 topic 誰先到不一定（重建時更是亂讀），全部套完的結果都一樣。
做法是第 7 集的「先記事實再推進」：每一欄只記事實（付了、拒單了、退款了）；
同一個階段的事實有先後排名，排名高的蓋過排名低的、反過來不會；整體狀態由這些事實推出來，不另外記。
"""
import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from functools import reduce
from typing import Any

from foodgo import contracts

# 投影聽得懂的事件：四個上下文的事實＋編排器的那一則
TRACKS: dict[str, type[contracts.IntegrationEvent]] = {
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
        contracts.MealReady,
        contracts.CourierAssigned,
        contracts.SagaEscalated,
    )
}

# 每個階段的事實，照「走得多遠」排名：只會往後走（退款了，遲到的「已授權」也蓋不回去）
PAYMENT = ("", "authorized", "failed", "refund_failed", "refunded")
KITCHEN = ("", "accepted", "rejected", "ready", "cancelled")

STATUS = {  # 整體狀態 → 畫面上的字
    "placed": "已下單",
    "paid": "已付款",
    "cooking": "製作中",
    "delivering": "外送中",
    "cancelling": "取消中",
    "cancelled": "已取消",
    "needs_attention": "人工處理中",
}
STOPPING = frozenset({"cancelling", "cancelled", "needs_attention"})  # 這張單做不成了


@dataclass(frozen=True)
class Tracking:
    """order_tracking 的一列：一張訂單，畫面要的全部，一次查完。"""

    order_id: str
    restaurant_id: str = ""  # OrderPlaced、DeliveryRequested
    items: tuple[contracts.OrderItem, ...] = ()  # OrderPlaced：菜名與份數
    note: str = ""  # OrderPlaced
    amount: int | None = None  # PaymentRequested：合計（元）
    delivery_address: str = ""  # DeliveryRequested
    placed_at: datetime | None = None  # OrderPlaced 發生的時間
    payment: str = ""  # PAYMENT 其中之一
    payment_id: str = ""
    payment_reason: str = ""  # 付款失敗、退款失敗的理由
    refunded: int | None = None  # PaymentRefunded：退了多少元（沒扣到款是 0）
    kitchen: str = ""  # KITCHEN 其中之一
    ticket_id: str = ""
    kitchen_reason: str = ""  # 拒單、撤銷的理由
    delivery: str = ""  # "" 或 assigned
    delivery_id: str = ""
    courier_id: str = ""
    cancelled: str = ""  # OrderCancelled 的理由；空字串＝沒有取消
    attention: str = ""  # SagaEscalated 的理由；空字串＝不用人工處理
    updated_at: datetime | None = None  # 套用過的事件裡，最晚發生的那一則的時間

    @property
    def status(self) -> str:
        """整體狀態：由事實推出來，不另外記（所以不會和事實對不上）。"""
        if self.cancelled:
            return "cancelled"
        if self.attention:
            return "needs_attention"
        if self.payment in ("failed", "refund_failed", "refunded") or self.kitchen in ("rejected", "cancelled"):
            return "cancelling"
        if self.delivery == "assigned":
            return "delivering"
        if self.kitchen in ("accepted", "ready"):
            return "cooking"
        if self.payment == "authorized":
            return "paid"
        return "placed"


@dataclass(frozen=True)
class StepView:
    """追蹤頁上的一格：點餐、付款、廚房、外送。"""

    key: str  # ordering／payment／kitchen／delivery（畫面用上下文的顏色）
    title: str
    state: str  # done 做完／active 進行中／failed 失敗／undone 補償了／todo 還沒輪到／skipped 不會做了
    detail: str


# ---------------------------------------------------------------- 投影
def decode(payload: str) -> contracts.IntegrationEvent | None:
    """Kafka 來的一則 JSON：投影要的就變回事件，其他（例如命令）回 None。"""
    kind = json.loads(payload)["type"]
    return TRACKS[kind].from_json(payload) if kind in TRACKS else None


def apply(state: Tracking | None, event: contracts.IntegrationEvent) -> Tracking:
    """一則事件套到追蹤上。state 是 None＝這張訂單第一次出現（不管第一則是誰的事件）。"""
    t = state or Tracking(event.order_id)
    if event.order_id != t.order_id:
        raise ValueError(f"{type(event).__name__} 是 {event.order_id} 的，不是 {t.order_id} 的")
    when = datetime.fromisoformat(event.occurred_at)
    match event:
        case contracts.OrderPlaced():
            t = replace(
                t,
                restaurant_id=_fill(t.restaurant_id, event.restaurant_id),
                items=_fill(t.items, event.items),
                note=_fill(t.note, event.note),
                placed_at=when if t.placed_at is None else min(t.placed_at, when),
            )
        case contracts.PaymentRequested():
            t = replace(t, amount=_fill(t.amount, event.amount))
        case contracts.DeliveryRequested():
            t = replace(
                t,
                restaurant_id=_fill(t.restaurant_id, event.restaurant_id),
                delivery_address=_fill(t.delivery_address, event.delivery_address),
            )
        case contracts.OrderCancelled():
            t = replace(t, cancelled=_fill(t.cancelled, event.reason))
        case contracts.PaymentAuthorized():
            t = _payment(t, "authorized", "", event.payment_id)
        case contracts.PaymentFailed():
            t = _payment(t, "failed", event.reason, event.payment_id)
        case contracts.PaymentRefunded():
            t = replace(_payment(t, "refunded", "", event.payment_id), refunded=_fill(t.refunded, event.amount))
        case contracts.RefundFailed():
            t = _payment(t, "refund_failed", event.reason, event.payment_id)
        case contracts.TicketAccepted():
            t = _kitchen(t, "accepted", "", event.ticket_id)
        case contracts.TicketRejected():
            t = _kitchen(t, "rejected", event.reason, event.ticket_id)
        case contracts.TicketCancelled():
            t = _kitchen(t, "cancelled", event.reason, event.ticket_id)
        case contracts.MealReady():
            t = replace(_kitchen(t, "ready", "", event.ticket_id), restaurant_id=_fill(t.restaurant_id, event.restaurant_id))
        case contracts.CourierAssigned():
            t = replace(
                t,
                delivery="assigned",
                delivery_id=_fill(t.delivery_id, event.delivery_id),
                courier_id=_fill(t.courier_id, event.courier_id),
            )
        case contracts.SagaEscalated():
            t = replace(t, attention=_fill(t.attention, event.reason))
        case _:
            raise TypeError(f"追蹤看不懂 {type(event).__name__}")
    return replace(t, updated_at=when if t.updated_at is None else max(t.updated_at, when))


def project(events: Iterable[contracts.IntegrationEvent], state: Tracking | None = None) -> Tracking | None:
    """一串事件從頭套到尾（和第 5 集的 fold 一樣是 reduce）。沒有事件就是 None。"""
    return reduce(apply, events, state)


def _fill(current: Any, new: Any) -> Any:
    """兩則事件記同一欄：有值的蓋過沒值的；兩邊都有又不一樣，取大的那個（結果才和誰先到無關）。"""
    if current is None or current == "" or current == ():
        return new
    if new is None or new == "" or new == () or new == current:
        return current
    return max(current, new)


def _later(ranks: tuple[str, ...], current: tuple[str, str], new: tuple[str, str]) -> tuple[str, str]:
    """同一個階段的兩個事實：排名高的留下；一樣高就照理由的字串選一個（一樣和順序無關）。"""
    return max(current, new, key=lambda fact: (ranks.index(fact[0]), fact[1]))


def _payment(t: Tracking, status: str, reason: str, payment_id: str) -> Tracking:
    status, reason = _later(PAYMENT, (t.payment, t.payment_reason), (status, reason))
    return replace(t, payment=status, payment_reason=reason, payment_id=_fill(t.payment_id, payment_id))


def _kitchen(t: Tracking, status: str, reason: str, ticket_id: str) -> Tracking:
    status, reason = _later(KITCHEN, (t.kitchen, t.kitchen_reason), (status, reason))
    return replace(t, kitchen=status, kitchen_reason=reason, ticket_id=_fill(t.ticket_id, ticket_id))


# ---------------------------------------------------------------- 給畫面看
def headline(t: Tracking) -> str:
    """狀態底下那一句話。"""
    match t.status:
        case "placed":
            return "訂單送出了，正在付款"
        case "paid":
            return "付款完成，等餐廳接單"
        case "cooking":
            return "餐廳接單了，正在做"
        case "delivering":
            return f"外送員 {t.courier_id} 接單了"
        case "cancelling":
            return "這張單做不成了，正在退款、取消"
        case "cancelled":
            return f"訂單已取消：{t.cancelled}"
        case _:
            return f"自動處理卡住了，客服會接手：{t.attention}"


def steps(t: Tracking) -> tuple[StepView, ...]:
    """步驟條的四格。每一格只看自己那個階段的事實，加上整張單是不是做不成了。"""
    stopping = t.status in STOPPING
    amount = f"{t.amount} 元" if t.amount is not None else ""

    if t.cancelled:
        ordering = ("undone", "已取消")
    elif t.placed_at is not None:
        ordering = ("done", "已送出")
    else:
        ordering = ("active", "訂單資料還在路上")

    if t.payment == "authorized" and t.status == "needs_attention":
        payment = ("failed", f"已付 {amount}，退款卡住了")
    elif t.payment == "authorized" and stopping:
        payment = ("active", f"已付 {amount}，退款中")
    elif t.payment == "authorized":
        payment = ("done", f"已付 {amount}（{t.payment_id}）")
    elif t.payment == "failed":
        payment = ("failed", f"付款失敗：{t.payment_reason}")
    elif t.payment == "refund_failed":
        payment = ("failed", f"退款失敗：{t.payment_reason}")
    elif t.payment == "refunded":
        payment = ("undone", f"已退款 {t.refunded} 元" if t.refunded else "沒有扣款，不用退")
    elif stopping:
        payment = ("skipped", "不用付了")
    else:
        payment = ("active", f"付款中 {amount}".strip())

    if t.kitchen == "accepted" and stopping:
        kitchen = ("active", f"{t.ticket_id} 撤銷中")
    elif t.kitchen == "accepted":
        kitchen = ("done", f"{t.ticket_id} 製作中")
    elif t.kitchen == "ready":
        kitchen = ("done", f"{t.ticket_id} 已出餐")
    elif t.kitchen == "rejected":
        kitchen = ("failed", f"餐廳拒單：{t.kitchen_reason}")
    elif t.kitchen == "cancelled":
        kitchen = ("undone", f"{t.ticket_id} 已撤銷")
    elif stopping:
        kitchen = ("skipped", "不用做了")
    elif t.payment == "authorized":
        kitchen = ("active", "等餐廳接單")
    else:
        kitchen = ("todo", "")

    if t.delivery == "assigned":
        delivery = ("done", f"外送員 {t.courier_id}（{t.delivery_id}）")
    elif stopping:
        delivery = ("skipped", "不用送了")
    elif t.kitchen in ("accepted", "ready"):
        delivery = ("active", "找外送員中")
    else:
        delivery = ("todo", "")

    return tuple(
        StepView(key, title, *view)
        for key, title, view in (
            ("ordering", "點餐", ordering),
            ("payment", "付款", payment),
            ("kitchen", "廚房", kitchen),
            ("delivery", "外送", delivery),
        )
    )
