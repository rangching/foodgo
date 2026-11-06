"""第 9 集：訂單追蹤的投影是純函式——一則事件進來，追蹤那一列變成什麼樣子。冪等、不管順序。

執行：uv run pytest tests/ep09 -q（這個檔不用 Docker：不碰資料庫、不碰 Kafka）
"""
import random

import pytest
from ep09_facts import (
    SCENARIOS,
    START,
    declined,
    happy,
    kitchen_crashed,
    placed,
    refund_failed,
    rejected,
)

from foodgo import contracts
from foodgo.tracking.projection import (
    STATUS,
    Tracking,
    apply,
    decode,
    headline,
    project,
    steps,
)


def states(t: Tracking) -> list[str]:
    return [s.state for s in steps(t)]


# ---------------------------------------------------------------- 一路走完：一列就有四個上下文的進度
def test_a001_all_the_way_in_one_row():
    t = project(happy())
    assert t.order_id == "A001"
    assert t.status == "delivering" and STATUS[t.status] == "外送中"
    assert [(i.name, i.quantity) for i in t.items] == [("牛肉麵", 1), ("紅茶", 1)]  # 點餐說的
    assert (t.restaurant_id, t.note, t.amount, t.delivery_address) == ("R007", "不要香菜", 230, "信義路五段 7 號")
    assert (t.payment, t.payment_id) == ("authorized", "P001")  # 付款說的
    assert (t.kitchen, t.ticket_id) == ("accepted", "T001")  # 廚房說的
    assert (t.delivery, t.delivery_id, t.courier_id) == ("assigned", "D001", "M01")  # 外送說的
    assert t.placed_at == START
    assert t.updated_at.isoformat() == "2026-11-06T06:50:03+00:00"  # 最晚那一則的時間
    assert states(t) == ["done", "done", "done", "done"]
    assert headline(t) == "外送員 M01 接單了"


@pytest.mark.parametrize(
    "count, status, view",
    [
        (1, "placed", ["done", "active", "todo", "todo"]),
        (3, "placed", ["done", "active", "todo", "todo"]),
        (4, "paid", ["done", "done", "active", "todo"]),
        (5, "cooking", ["done", "done", "done", "active"]),
        (6, "delivering", ["done", "done", "done", "done"]),
    ],
)
def test_the_status_moves_forward_one_fact_at_a_time(count, status, view):
    t = project(happy()[:count])
    assert t.status == status
    assert states(t) == view


def test_what_each_step_says():
    assert [s.detail for s in steps(project(happy()))] == ["已送出", "已付 230 元（P001）", "T001 製作中", "外送員 M01（D001）"]
    assert [s.title for s in steps(project(happy()))] == ["點餐", "付款", "廚房", "外送"]
    assert [s.key for s in steps(project(happy()))] == ["ordering", "payment", "kitchen", "delivery"]


# ---------------------------------------------------------------- 冪等：同一則套兩次，和套一次一樣
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_same_event_twice_changes_nothing(scenario):
    events = SCENARIOS[scenario]()
    once = project(events)
    twice = project(e for event in events for e in (event, event))
    assert twice == once


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_replaying_everything_on_top_changes_nothing(scenario):
    events = SCENARIOS[scenario]()
    once = project(events)
    assert project(events, once) == once  # 重建時已經有的列又被重播一次：還是一樣


def test_a_repeated_reply_with_a_new_event_id_is_still_one_fact():
    first, again = kitchen_crashed()[6], kitchen_crashed()[7]
    assert type(first) is type(again) is contracts.TicketAccepted
    assert first.event_id != again.event_id  # 參與者被重送命令時「再回一次」：新的編號、同一件事
    base = project(placed())
    assert apply(base, first) == apply(apply(base, first), again)


# ---------------------------------------------------------------- 不管順序：五條 topic 誰先到不一定
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_any_order_gives_the_same_row(scenario):
    events = SCENARIOS[scenario]()
    expected = project(events)
    assert project(reversed(events)) == expected
    shuffle = random.Random(9)
    for _ in range(50):
        mixed = events[:]
        shuffle.shuffle(mixed)
        assert project(mixed) == expected


def test_payment_can_arrive_before_the_order():
    """付款的 topic 先讀到：這一列先有付款，品項、金額還在路上；點餐的事件到了再補上。"""
    authorized = happy()[3]
    t = apply(None, authorized)
    assert (t.order_id, t.payment, t.items, t.amount, t.placed_at) == ("A001", "authorized", (), None, None)
    assert t.status == "paid"
    assert steps(t)[0].state == "active" and steps(t)[0].detail == "訂單資料還在路上"
    assert project(placed(), t) == project([*placed(), authorized])


def test_a_late_fact_never_moves_a_step_backwards():
    """廚房當機：撤銷之後才到的「已接單」（第 8 集的 crash），蓋不回去。"""
    events = kitchen_crashed()
    t = project(events)
    assert (t.kitchen, t.payment, t.status) == ("cancelled", "refunded", "cancelled")
    cancelled_first = [e for e in events if not isinstance(e, contracts.TicketAccepted)] + [events[6], events[7]]
    assert project(cancelled_first) == t


# ---------------------------------------------------------------- 失敗的情境
def test_a_rejected_order_shows_the_refund_and_the_cancel():
    t = project(rejected())
    assert t.status == "cancelled" and STATUS[t.status] == "已取消"
    assert (t.payment, t.refunded, t.kitchen, t.kitchen_reason) == ("refunded", 230, "rejected", "餐廳太忙，暫停接單")
    assert t.cancelled == "餐廳拒單：餐廳太忙，暫停接單"
    assert [(s.state, s.detail) for s in steps(t)] == [
        ("undone", "已取消"),
        ("undone", "已退款 230 元"),
        ("failed", "餐廳拒單：餐廳太忙，暫停接單"),
        ("skipped", "不用送了"),
    ]
    assert headline(t) == "訂單已取消：餐廳拒單：餐廳太忙，暫停接單"


def test_while_compensating_the_page_says_cancelling():
    t = project(rejected()[:5])  # 拒單到了，退款還沒到
    assert t.status == "cancelling" and STATUS[t.status] == "取消中"
    assert [(s.state, s.detail) for s in steps(t)][1] == ("active", "已付 230 元，退款中")


def test_a_declined_payment():
    t = project(declined())
    assert (t.status, t.payment, t.payment_reason) == ("cancelled", "failed", "卡片額度不足")
    assert states(t) == ["undone", "failed", "skipped", "skipped"]


def test_a_refund_that_keeps_failing_needs_attention():
    t = project(refund_failed())
    assert t.status == "needs_attention" and STATUS[t.status] == "人工處理中"
    assert t.attention == "RefundPayment 試了 2 次都失敗（發卡銀行暫時無法退款）"
    assert t.cancelled == ""  # 錢還沒退，訂單沒有被默默取消
    assert [(s.state, s.detail) for s in steps(t)][:3] == [
        ("done", "已送出"),
        ("failed", "退款失敗：發卡銀行暫時無法退款"),
        ("failed", "餐廳拒單：餐廳太忙，暫停接單"),
    ]
    assert headline(t).startswith("自動處理卡住了，客服會接手")


def test_before_the_escalation_it_is_still_cancelling():
    t = project(refund_failed()[:-1])  # 退款失敗了，但編排器還沒放棄（它會重試）
    assert t.status == "cancelling"  # 投影不自己猜「要人工處理了沒」：等編排器說


def test_a_refund_of_nothing():
    t = apply(project(placed()), contracts.PaymentRefunded(order_id="A001", payment_id="", amount=0))
    assert (t.payment, t.refunded) == ("refunded", 0)
    assert steps(t)[1].detail == "沒有扣款，不用退"


def test_a_meal_ready_moves_the_kitchen_forward():
    t = apply(project(happy()), contracts.MealReady(order_id="A001", ticket_id="T001", restaurant_id="R007"))
    assert (t.kitchen, t.status) == ("ready", "delivering")
    assert steps(t)[2].detail == "T001 已出餐"


# ---------------------------------------------------------------- 邊邊角角
def test_every_status_has_words():
    assert set(STATUS) == {"placed", "paid", "cooking", "delivering", "cancelling", "cancelled", "needs_attention"}


def test_an_event_of_another_order_is_refused():
    with pytest.raises(ValueError):
        apply(project(happy("A001")), happy("A002")[3])


def test_decode_keeps_only_what_tracking_needs():
    assert decode(happy()[3].to_json()) == happy()[3]
    assert decode(contracts.AuthorizePayment(order_id="A001", customer_id="C042", amount=230).to_json()) is None
    escalated = refund_failed()[-1]
    assert decode(escalated.to_json()) == escalated  # 編排器公告的那一則也聽


def test_nothing_to_project():
    assert project([]) is None
