"""第 5 集：狀態是事件的總和——evolve(state, event)、fold(events)，版本號跟著算出來。

執行：uv run pytest tests/ep05 -q（不用 Docker）
"""
import dataclasses
import inspect
import subprocess
import sys

import pytest

from foodgo.ordering.events import ItemAdded, OrderCancelled, OrderDrafted
from foodgo.ordering.fold import OrderState, evolve, fold
from foodgo.ordering.model import Address, Money, Order, OrderLine, OrderStatus
from foodgo.shared.events import DomainEvent

A001_LINES = (OrderLine("牛肉麵", Money(160)), OrderLine("紅茶", Money(40)))


# ---------------------------------------------------------------- fold：從事件算出狀態
def test_fold_builds_a001_from_its_events(a001_events):
    state = fold(a001_events[:4])  # 送出那一刻
    assert isinstance(state, OrderState)
    assert state.order_id == "A001"
    assert state.customer_id == "C042" and state.restaurant_id == "R007"
    assert state.delivery_address == Address("信義路五段 7 號")
    assert state.delivery_fee == Money(30)
    assert state.note == "不要香菜"
    assert state.lines == A001_LINES
    assert state.status is OrderStatus.PLACED
    assert state.total == Money(230)
    assert state.version == 4


def test_the_last_event_decides_where_the_order_is(a001_events):
    state = fold(a001_events)
    assert state.status is OrderStatus.CANCELLED
    assert state.version == 5
    assert state.lines == A001_LINES  # 取消不會把品項變不見


def test_version_is_how_many_events_have_been_applied(a001_events):
    assert [fold(a001_events[:n]).version for n in range(1, 6)] == [1, 2, 3, 4, 5]
    assert [fold(a001_events[:n]).status.value for n in range(1, 6)] == ["draft", "draft", "draft", "placed", "cancelled"]
    assert [fold(a001_events[:n]).total.amount for n in range(1, 6)] == [30, 190, 230, 230, 230]


def test_no_events_means_no_order():
    assert fold([]) is None


def test_fold_is_evolve_over_and_over(a001_events):
    state = None
    for event in a001_events:
        state = evolve(state, event)
    assert state == fold(a001_events)


# ---------------------------------------------------------------- 純函式：不改舊狀態、不碰外面
def test_evolve_returns_a_new_state_and_leaves_the_old_one_alone(a001_events):
    before = fold(a001_events[:3])
    after = evolve(before, a001_events[3])
    assert before.status is OrderStatus.DRAFT and before.version == 3
    assert after.status is OrderStatus.PLACED and after.version == 4
    assert after is not before


def test_state_cannot_be_changed(a001_events):
    state = fold(a001_events)
    assert dataclasses.is_dataclass(OrderState) and OrderState.__dataclass_params__.frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.status = OrderStatus.DRAFT  # type: ignore[misc]


def test_folding_twice_gives_the_same_answer(a001_events):
    assert fold(a001_events) == fold(a001_events)
    assert fold(iter(a001_events)) == fold(a001_events)  # 給產生器也可以：一次讀一個事件


def test_fold_needs_no_database_no_kafka_no_web():
    code = (
        "import sys, foodgo.ordering.fold; "
        "print(sorted(m for m in ('psycopg', 'confluent_kafka', 'fastapi', 'foodgo.infra', 'foodgo.contracts') if m in sys.modules))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


# ---------------------------------------------------------------- 事件要夠重建整個聚合
def test_the_events_carry_everything_the_order_needs():
    """Order 的建構參數都在 OrderDrafted 裡、add_line 的參數都在 ItemAdded 裡：第 6 集才能只靠事件把 Order 擺回來。"""
    drafted = {f.name for f in dataclasses.fields(OrderDrafted)}
    added = {f.name for f in dataclasses.fields(ItemAdded)}
    assert set(inspect.signature(Order.__init__).parameters) - {"self"} <= drafted
    assert set(inspect.signature(Order.add_line).parameters) - {"self"} <= added


def test_fold_agrees_with_the_order_aggregate(a001_events):
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
    order.cancel("顧客改變心意")

    state = fold(a001_events)
    for field in ["order_id", "customer_id", "restaurant_id", "delivery_address", "delivery_fee", "note", "lines", "status", "total"]:
        assert getattr(state, field) == getattr(order, field), field


def test_the_new_events_stay_inside_ordering(a001_events):
    """OrderDrafted、ItemAdded 是點餐自己的事：不翻成整合事件，不會寫進 outbox、送到 Kafka。"""
    from foodgo.ordering.integration import to_integration

    assert [to_integration(event) is None for event in a001_events] == [True, True, True, False, True]


# ---------------------------------------------------------------- 認不得的事件不要默默略過
def test_the_first_event_must_draft_the_order():
    with pytest.raises(ValueError, match="OrderDrafted"):
        evolve(None, OrderCancelled(order_id="A001", reason="顧客改變心意"))


def test_an_order_is_drafted_only_once(a001_events):
    with pytest.raises(ValueError, match="A001"):
        fold([*a001_events[:2], a001_events[0]])


def test_an_unknown_event_is_refused(a001_events):
    @dataclasses.dataclass(frozen=True, kw_only=True)
    class SomethingHappened(DomainEvent):
        """不知道哪來的事件"""

        order_id: str

    with pytest.raises(TypeError, match="SomethingHappened"):
        fold([*a001_events, SomethingHappened(order_id="A001")])
