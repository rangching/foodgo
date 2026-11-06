"""第 8 集：OrderSaga 的狀態機——狀態、轉移、補償的順序、逾時與重試、補償失敗。

執行：uv run pytest tests/ep08 -q（這個檔不用 Docker：狀態機不碰資料庫、不碰 Kafka，時間也是測試給的）
"""
from datetime import timedelta

import pytest

from foodgo import contracts
from foodgo.saga.machine import FINISHED, FORWARD, OrderSaga, SagaState, Settings

from ep08_world import START, facts

SETTINGS = Settings(timeout=3, retries=1)


def started() -> OrderSaga:
    """收齊 A001 的三則事實：已經送出 AuthorizePayment。"""
    saga = OrderSaga("A001")
    for message in facts():
        saga.receive(message, START, SETTINGS)
    return saga


def sent(saga: OrderSaga) -> list[str]:
    commands, _ = saga.pull()
    return [type(c).__name__ for c in commands]


def at(seconds: float):
    return START + timedelta(seconds=seconds)


AUTHORIZED = contracts.PaymentAuthorized(order_id="A001", payment_id="P001")
ACCEPTED = contracts.TicketAccepted(order_id="A001", ticket_id="T001")
REJECTED = contracts.TicketRejected(order_id="A001", ticket_id="T001", reason="餐廳太忙，暫停接單")
ASSIGNED = contracts.CourierAssigned(order_id="A001", delivery_id="D001", courier_id="M01")
REFUNDED = contracts.PaymentRefunded(order_id="A001", payment_id="P001", amount=230)
REFUND_FAILED = contracts.RefundFailed(order_id="A001", payment_id="P001", reason="發卡銀行暫時無法退款")
CANCELLED = contracts.OrderCancelled(order_id="A001", reason="餐廳拒單：餐廳太忙，暫停接單")


# ---------------------------------------------------------------- 狀態
def test_the_states():
    assert [s.value for s in SagaState] == [
        "started",
        "payment_pending",
        "kitchen_pending",
        "delivery_pending",
        "completed",
        "compensating",
        "cancelled",
        "needs_attention",
    ]
    assert FINISHED == {SagaState.COMPLETED, SagaState.CANCELLED, SagaState.NEEDS_ATTENTION}


def test_forward_steps_and_their_compensations():
    assert [(s.command, s.done, s.failed, s.undo) for s in FORWARD] == [
        ("AuthorizePayment", "PaymentAuthorized", "PaymentFailed", "RefundPayment"),
        ("AcceptTicket", "TicketAccepted", "TicketRejected", "CancelTicket"),
        ("AssignCourier", "CourierAssigned", "", ""),
    ]


# ---------------------------------------------------------------- 出發：收齊三則事實
def test_the_saga_waits_for_all_three_facts():
    saga = OrderSaga("A001")
    order_placed, payment_requested, delivery_requested = facts()
    saga.receive(order_placed, START, SETTINGS)
    saga.receive(payment_requested, START, SETTINGS)
    assert saga.state is SagaState.STARTED and sent(saga) == []  # 還不知道送到哪裡
    saga.receive(delivery_requested, START, SETTINGS)
    assert saga.state is SagaState.PAYMENT_PENDING
    [command] = saga.pull()[0]
    assert command == contracts.AuthorizePayment(
        event_id=command.event_id, occurred_at=command.occurred_at, order_id="A001", customer_id="C042", amount=230
    )
    assert (saga.waiting_for, saga.attempts, saga.deadline) == ("AuthorizePayment", 1, at(3))


# ---------------------------------------------------------------- 正常流程
def test_the_happy_path():
    saga = started()
    assert sent(saga) == ["AuthorizePayment"]
    saga.receive(AUTHORIZED, at(1), SETTINGS)
    assert (saga.state, sent(saga)) == (SagaState.KITCHEN_PENDING, ["AcceptTicket"])
    saga.receive(ACCEPTED, at(2), SETTINGS)
    assert saga.state is SagaState.DELIVERY_PENDING
    [assign] = saga.pull()[0]
    assert (assign.restaurant_id, assign.delivery_address) == ("R007", "信義路五段 7 號")
    saga.receive(ASSIGNED, at(3), SETTINGS)
    assert saga.state is SagaState.COMPLETED
    assert saga.done == ["AuthorizePayment", "AcceptTicket", "AssignCourier"]
    assert {k: saga.data[k] for k in ("payment_id", "ticket_id", "delivery_id", "courier_id")} == {
        "payment_id": "P001",
        "ticket_id": "T001",
        "delivery_id": "D001",
        "courier_id": "M01",
    }


def test_the_kitchen_gets_the_dishes_but_no_price():
    saga = started()
    saga.pull()
    saga.receive(AUTHORIZED, at(1), SETTINGS)
    [accept] = saga.pull()[0]
    assert accept.items == (
        contracts.OrderItem(name="牛肉麵", quantity=1),
        contracts.OrderItem(name="紅茶", quantity=1),
    )
    assert "amount" not in accept.to_json() and "230" not in accept.to_json()


def test_the_log_tells_the_story():
    saga = started()
    for message in (AUTHORIZED, ACCEPTED, ASSIGNED):
        saga.receive(message, at(1), SETTINGS)
    _, log = saga.pull()
    assert [(e.state.value, e.action, e.message) for e in log] == [
        ("started", "receive", "OrderPlaced"),
        ("started", "receive", "PaymentRequested"),
        ("started", "receive", "DeliveryRequested"),
        ("payment_pending", "send", "AuthorizePayment"),
        ("payment_pending", "receive", "PaymentAuthorized"),
        ("kitchen_pending", "send", "AcceptTicket"),
        ("kitchen_pending", "receive", "TicketAccepted"),
        ("delivery_pending", "send", "AssignCourier"),
        ("delivery_pending", "receive", "CourierAssigned"),
        ("completed", "finish", ""),
    ]
    assert log[3].detail == "C042 付 230 元"


# ---------------------------------------------------------------- 失敗：倒回去
def test_a_declined_payment_just_cancels_the_order():
    saga = started()
    saga.pull()
    saga.receive(contracts.PaymentFailed(order_id="A001", payment_id="P001", reason="卡片額度不足"), at(1), SETTINGS)
    assert saga.state is SagaState.COMPENSATING
    [cancel] = saga.pull()[0]
    assert (type(cancel).__name__, cancel.reason) == ("CancelOrder", "付款失敗：卡片額度不足")  # 沒有扣到錢：不用退
    saga.receive(contracts.OrderCancelled(order_id="A001", reason=cancel.reason), at(2), SETTINGS)
    assert saga.state is SagaState.CANCELLED


def test_a_rejected_ticket_refunds_then_cancels():
    """付款成功但餐廳拒單：後做的先補——先退款，再取消訂單。"""
    saga = started()
    saga.receive(AUTHORIZED, at(1), SETTINGS)
    saga.pull()
    saga.receive(REJECTED, at(2), SETTINGS)
    assert (saga.state, saga.reason) == (SagaState.COMPENSATING, "餐廳拒單：餐廳太忙，暫停接單")
    assert sent(saga) == ["RefundPayment"]
    saga.receive(REFUNDED, at(3), SETTINGS)
    assert sent(saga) == ["CancelOrder"]
    saga.receive(CANCELLED, at(4), SETTINGS)
    assert saga.state is SagaState.CANCELLED
    assert saga.todo == [] and saga.waiting_for == ""


# ---------------------------------------------------------------- 逾時與重試
def test_no_answer_in_time_means_one_retry():
    saga = started()
    saga.receive(AUTHORIZED, at(1), SETTINGS)
    saga.pull()
    saga.check_deadline(at(3.5), SETTINGS)  # 還沒到：AcceptTicket 是 1 秒送的，等到 4 秒
    assert sent(saga) == []
    saga.check_deadline(at(4), SETTINGS)
    assert (saga.state, saga.attempts, saga.deadline) == (SagaState.KITCHEN_PENDING, 2, at(7))
    assert sent(saga) == ["AcceptTicket"]  # 同一個命令，新的一則


def test_an_answer_during_the_retry_carries_on():
    """廚房很慢：第一次逾時、重送；第一則的回覆在重送之後才到——照樣往下走，第二則的回覆就是重複的。"""
    saga = started()
    saga.receive(AUTHORIZED, at(1), SETTINGS)
    saga.check_deadline(at(4), SETTINGS)
    saga.receive(ACCEPTED, at(5), SETTINGS)
    assert saga.state is SagaState.DELIVERY_PENDING
    saga.receive(contracts.TicketAccepted(order_id="A001", ticket_id="T001"), at(6), SETTINGS)  # 重送那則的回覆
    assert saga.state is SagaState.DELIVERY_PENDING
    _, log = saga.pull()
    assert [e.action for e in log][-2:] == ["send", "ignore"]


def test_still_no_answer_after_the_retry_means_compensate():
    saga = started()
    saga.receive(AUTHORIZED, at(1), SETTINGS)
    saga.pull()
    saga.check_deadline(at(4), SETTINGS)
    saga.check_deadline(at(7), SETTINGS)
    assert saga.state is SagaState.COMPENSATING
    assert saga.reason == "廚房沒有回覆：送了 2 次，各等 3 秒"
    # 廚房有沒有接單不確定：CancelTicket 先送、不等；接著退款（等回覆），最後取消訂單
    assert sent(saga) == ["AcceptTicket", "CancelTicket", "RefundPayment"]
    assert (saga.waiting_for, saga.todo) == ("RefundPayment", ["CancelOrder"])


def test_the_payment_step_times_out_too():
    saga = started()
    saga.pull()
    saga.check_deadline(at(3), SETTINGS)
    saga.check_deadline(at(6), SETTINGS)
    assert sent(saga) == ["AuthorizePayment", "RefundPayment", "CancelOrder"]  # 付款有沒有扣款不確定：退款先送、不等
    assert saga.waiting_for == "CancelOrder"


def test_retries_and_timeout_come_from_the_environment(monkeypatch):
    assert Settings.from_environment() == Settings(timeout=3, retries=1)
    monkeypatch.setenv("FOODGO_SAGA_TIMEOUT", "0.5")
    monkeypatch.setenv("FOODGO_SAGA_RETRIES", "2")
    assert Settings.from_environment() == Settings(timeout=0.5, retries=2)


# ---------------------------------------------------------------- 補償本身失敗
def rejected() -> OrderSaga:
    saga = started()
    saga.receive(AUTHORIZED, at(1), SETTINGS)
    saga.receive(REJECTED, at(2), SETTINGS)
    saga.pull()
    return saga


def test_a_failed_refund_is_retried():
    saga = rejected()
    saga.receive(REFUND_FAILED, at(3), SETTINGS)
    assert (saga.state, saga.attempts, sent(saga)) == (SagaState.COMPENSATING, 2, ["RefundPayment"])
    saga.receive(REFUNDED, at(4), SETTINGS)  # 第二次成功了
    assert sent(saga) == ["CancelOrder"]


def test_a_refund_that_keeps_failing_needs_attention():
    saga = rejected()
    saga.receive(REFUND_FAILED, at(3), SETTINGS)
    saga.receive(REFUND_FAILED, at(4), SETTINGS)
    assert saga.state is SagaState.NEEDS_ATTENTION
    commands, log = saga.pull()
    assert [type(c).__name__ for c in commands] == ["RefundPayment"]  # 不會默默往下取消訂單：錢還沒退
    assert (log[-1].action, log[-1].detail) == (
        "give_up",
        "要人工處理：RefundPayment 試了 2 次都失敗（發卡銀行暫時無法退款）",
    )
    assert (saga.waiting_for, saga.deadline) == ("", None)


def test_a_refund_without_answer_needs_attention():
    saga = rejected()
    saga.check_deadline(at(5), SETTINGS)
    saga.check_deadline(at(8), SETTINGS)
    assert saga.state is SagaState.NEEDS_ATTENTION


# ---------------------------------------------------------------- 重複、遲到、顧客自己取消
def test_duplicates_and_late_answers_are_ignored():
    saga = started()
    saga.receive(AUTHORIZED, at(1), SETTINGS)
    saga.receive(AUTHORIZED, at(1), SETTINGS)
    assert saga.state is SagaState.KITCHEN_PENDING
    saga.receive(ASSIGNED, at(2), SETTINGS)  # 沒送過 AssignCourier
    assert saga.state is SagaState.KITCHEN_PENDING
    _, log = saga.pull()
    assert [e.action for e in log][-2:] == ["ignore", "ignore"]


def test_a_customer_cancellation_is_compensated_without_cancelling_twice():
    saga = started()
    saga.receive(AUTHORIZED, at(1), SETTINGS)
    saga.pull()
    saga.receive(contracts.OrderCancelled(order_id="A001", reason="顧客改變心意"), at(2), SETTINGS)
    # 廚房有沒有接單不確定 → CancelTicket 先送；付款做成了 → 退款；訂單已經取消了 → 不再送 CancelOrder
    assert sent(saga) == ["CancelTicket", "RefundPayment"]
    saga.receive(REFUNDED, at(3), SETTINGS)
    assert saga.state is SagaState.CANCELLED and sent(saga) == []


@pytest.mark.parametrize("state", sorted(FINISHED))
def test_finished_sagas_stay_finished(state):
    saga = OrderSaga("A001", state=state)
    for message in (AUTHORIZED, ACCEPTED, REJECTED, CANCELLED):
        saga.receive(message, at(1), SETTINGS)
    saga.check_deadline(at(100), SETTINGS)
    assert saga.state is state and sent(saga) == []
