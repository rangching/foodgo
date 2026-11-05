"""第 5 集：事件儲存（記憶體版）——事件流、版本號、樂觀併發。第 6 集換成 PostgreSQL 的 events 表。

執行：uv run pytest tests/ep05 -q（不用 Docker）
"""
import dataclasses

import pytest

from foodgo.ordering.events import ItemAdded, OrderCancelled
from foodgo.ordering.fold import fold
from foodgo.ordering.model import Money, OrderStatus
from foodgo.shared.event_store import ConcurrencyConflict, InMemoryEventStore, RecordedEvent


@pytest.fixture
def store(a001_events) -> InMemoryEventStore:
    """A001 已經送出：事件流裡有 4 個事件。"""
    store = InMemoryEventStore()
    store.append("A001", a001_events[:4], expected_version=0)
    return store


# ---------------------------------------------------------------- 事件流：一張訂單一條，只往後加
def test_a_new_stream_starts_at_version_zero():
    store = InMemoryEventStore()
    assert store.version("A001") == 0
    assert store.load("A001") == []


def test_append_numbers_each_event(a001_events):
    store = InMemoryEventStore()
    assert store.append("A001", a001_events[:4], expected_version=0) == 4
    records = store.records("A001")
    assert [r.version for r in records] == [1, 2, 3, 4]
    assert [r.type for r in records] == ["OrderDrafted", "ItemAdded", "ItemAdded", "OrderPlaced"]
    assert {r.stream_id for r in records} == {"A001"}
    assert [r.occurred_at for r in records] == [e.occurred_at for e in a001_events[:4]]
    assert [r.event for r in records] == a001_events[:4]


def test_load_then_fold_gives_the_state_and_its_version(store):
    state = fold(store.load("A001"))
    assert state.status is OrderStatus.PLACED
    assert state.total == Money(230)
    assert state.version == store.version("A001") == 4


def test_streams_do_not_mix(store, a001_events):
    store.append("A002", a001_events[:1], expected_version=0)
    assert store.version("A001") == 4
    assert store.version("A002") == 1
    assert [r.stream_id for r in store.records("A002")] == ["A002"]


def test_the_store_is_append_only(store):
    assert not [name for name in ("update", "delete", "remove", "clear") if hasattr(store, name)]
    assert dataclasses.is_dataclass(RecordedEvent) and RecordedEvent.__dataclass_params__.frozen
    store.load("A001").clear()  # 拿到的是複本：外面弄亂了也不影響儲存
    store.records("A001").clear()
    assert store.version("A001") == 4


# ---------------------------------------------------------------- 樂觀併發：expected_version 對不上就不寫
def test_appending_with_the_right_version_moves_it_forward(store):
    cancel = OrderCancelled(order_id="A001", reason="顧客改變心意")
    assert store.append("A001", [cancel], expected_version=4) == 5
    assert fold(store.load("A001")).status is OrderStatus.CANCELLED


def test_two_people_cancel_at_once_only_one_wins(store):
    seen_by_customer = fold(store.load("A001")).version  # 顧客 App 讀到第 4 版
    seen_by_support = fold(store.load("A001")).version  # 客服後台也讀到第 4 版

    store.append("A001", [OrderCancelled(order_id="A001", reason="顧客改變心意")], expected_version=seen_by_customer)
    with pytest.raises(ConcurrencyConflict) as conflict:
        store.append("A001", [OrderCancelled(order_id="A001", reason="餐廳太忙")], expected_version=seen_by_support)

    assert (conflict.value.stream_id, conflict.value.expected, conflict.value.actual) == ("A001", 4, 5)
    assert "A001" in str(conflict.value)
    assert [r.type for r in store.records("A001")].count("OrderCancelled") == 1  # 不變條件：只取消一次


def test_a_conflict_writes_nothing_at_all(store):
    late = [ItemAdded(order_id="A001", name="滷蛋", unit_price=Money(15), quantity=1)] * 2
    with pytest.raises(ConcurrencyConflict):
        store.append("A001", late, expected_version=3)
    assert store.version("A001") == 4  # 整批都沒寫進去


def test_a_new_stream_must_expect_version_zero(a001_events):
    store = InMemoryEventStore()
    with pytest.raises(ConcurrencyConflict) as conflict:
        store.append("A009", a001_events[:1], expected_version=1)
    assert (conflict.value.expected, conflict.value.actual) == (1, 0)
    assert store.version("A009") == 0


def test_the_same_new_stream_cannot_be_created_twice(store, a001_events):
    with pytest.raises(ConcurrencyConflict):
        store.append("A001", a001_events[:1], expected_version=0)  # 另一個人也以為 A001 還不存在
