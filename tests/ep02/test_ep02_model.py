"""第 2 集：把規則寫進模型——值物件、聚合與不變條件、領域事件、Repository。

執行：uv run pytest tests/ep02 -q
"""
import dataclasses
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from foodgo.ordering.events import ItemAdded, OrderCancelled, OrderDrafted, OrderPlaced
from foodgo.ordering.model import (
    Address,
    Money,
    Order,
    OrderLine,
    OrderRuleViolation,
    OrderStatus,
)
from foodgo.ordering.repository import InMemoryOrderRepository, OrderRepository

ROOT = Path(__file__).resolve().parents[2]


def a001() -> Order:
    """範例訂單 A001：牛肉麵 ×1 160、紅茶 ×1 40、外送費 30（還沒送出）。"""
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
    return order


# ---------------------------------------------------------------- 值物件
def test_money_is_compared_by_value():
    assert Money(160) == Money(160)
    assert Money(160) is not Money(160)
    assert Money(160) + Money(40) == Money(200)
    assert Money(40) * 2 == Money(80)


def test_money_cannot_be_changed():
    price = Money(160)
    with pytest.raises(dataclasses.FrozenInstanceError):
        price.amount = 1  # type: ignore[misc]


@pytest.mark.parametrize("bad", [-1, 1.5])
def test_money_is_a_whole_non_negative_amount(bad):
    with pytest.raises(ValueError):
        Money(bad)


def test_address_is_compared_by_value():
    assert Address("信義路五段 7 號") == Address("信義路五段 7 號")
    assert Address("信義路五段 7 號") != Address("信義路五段 8 號")
    with pytest.raises(dataclasses.FrozenInstanceError):
        Address("信義路五段 7 號").street = "別的地方"  # type: ignore[misc]
    with pytest.raises(ValueError):
        Address("  ")


def test_order_line_knows_its_subtotal():
    assert OrderLine("紅茶", Money(40), 2).subtotal == Money(80)
    with pytest.raises(ValueError):
        OrderLine("紅茶", Money(40), 0)


# ---------------------------------------------------------------- 聚合與不變條件
def test_a001_adds_up_to_230():
    order = a001()
    assert order.subtotal == Money(200)
    assert order.total == Money(230)


def test_order_is_an_entity_compared_by_id():
    other = Order(
        order_id="A001",
        customer_id="C999",
        restaurant_id="R001",
        delivery_address=Address("別的地方"),
        delivery_fee=Money(0),
    )
    assert a001() == other
    assert a001() != Order(
        order_id="A002",
        customer_id="C042",
        restaurant_id="R007",
        delivery_address=Address("信義路五段 7 號"),
        delivery_fee=Money(30),
    )


def test_cannot_place_an_empty_order():
    order = Order(
        order_id="A001",
        customer_id="C042",
        restaurant_id="R007",
        delivery_address=Address("信義路五段 7 號"),
        delivery_fee=Money(30),
    )
    with pytest.raises(OrderRuleViolation):
        order.place()
    assert order.status is OrderStatus.DRAFT


def test_cannot_change_lines_after_placed():
    order = a001()
    order.place()
    with pytest.raises(OrderRuleViolation):
        order.add_line("滷蛋", Money(15))
    assert len(order.lines) == 2
    assert order.total == Money(230)


def test_cannot_place_twice():
    order = a001()
    order.place()
    with pytest.raises(OrderRuleViolation):
        order.place()


def test_rules_cannot_be_bypassed_from_outside():
    order = a001()
    order.place()
    with pytest.raises(AttributeError):
        order.status = OrderStatus.DRAFT  # type: ignore[misc]
    with pytest.raises(AttributeError):
        order.lines.append(OrderLine("滷蛋", Money(15)))  # type: ignore[attr-defined]


def test_cancel_a_placed_order():
    order = a001()
    order.place()
    order.cancel("顧客改變心意")
    assert order.status is OrderStatus.CANCELLED


@pytest.mark.parametrize(
    "prepare, reason",
    [
        (lambda o: None, "還沒送出"),  # 草稿還沒成立，不叫取消
        (lambda o: (o.place(), o.cancel("第一次")), "取消兩次"),  # 已取消就不能再取消
        (lambda o: o.place(), ""),  # 取消一定要有理由
    ],
    ids=["draft", "twice", "no-reason"],
)
def test_cancel_rules(prepare, reason):
    order = a001()
    prepare(order)
    with pytest.raises(OrderRuleViolation):
        order.cancel(reason)


# ---------------------------------------------------------------- 領域事件
def test_aggregate_collects_events_and_hands_them_out_once():
    order = a001()
    # 第 6 集起，建立草稿、加品項也各記一個事件（事件溯源要靠它們把訂單算回來）
    assert [type(e) for e in order.pull_events()] == [OrderDrafted, ItemAdded, ItemAdded]
    order.place()
    events = order.pull_events()
    assert len(events) == 1 and isinstance(events[0], OrderPlaced)
    assert (events[0].order_id, events[0].customer_id, events[0].restaurant_id) == ("A001", "C042", "R007")
    assert order.pull_events() == []


def test_cancel_records_order_cancelled():
    order = a001()
    order.place()
    order.cancel("顧客改變心意")
    *_, placed, cancelled = order.pull_events()  # 前面是建立草稿、加品項的事件（第 6 集起）
    assert isinstance(placed, OrderPlaced)
    assert isinstance(cancelled, OrderCancelled) and cancelled.reason == "顧客改變心意"


def test_broken_rule_records_no_event():
    order = a001()
    order.place()
    order.pull_events()
    with pytest.raises(OrderRuleViolation):
        order.place()
    assert order.pull_events() == []


# ---------------------------------------------------------------- Repository
def test_in_memory_repository_round_trip():
    repo: OrderRepository = InMemoryOrderRepository()
    assert repo.next_id() == "A001"
    assert repo.next_id() == "A002"
    order = a001()
    repo.save(order)
    assert repo.get("A001") is order
    assert repo.get("A999") is None


# ---------------------------------------------------------------- 邊界
def test_model_does_not_know_the_web():
    code = "import sys, foodgo.ordering.model; print(sorted({'fastapi', 'pydantic'} & set(sys.modules)))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


def test_glossary_writes_down_the_rules():
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    for term in ["Money", "Address", "OrderLine", "不變條件", "cancel()"]:
        assert term in glossary


def lint_imports(cwd, env=None):
    return subprocess.run(["lint-imports"], cwd=cwd, env=env, capture_output=True, text=True)


def test_contexts_do_not_know_the_app():
    result = lint_imports(ROOT)
    assert result.returncode == 0, result.stdout
    assert "上下文不認識組裝點 KEPT" in result.stdout


def test_importing_the_app_from_a_context_is_caught(tmp_path):
    shutil.copytree(ROOT / "src", tmp_path / "src")
    shutil.copy(ROOT / ".importlinter", tmp_path)
    leak = tmp_path / "src" / "foodgo" / "ordering" / "leak.py"
    leak.write_text("from foodgo.app import app\n", encoding="utf-8")

    env = {**os.environ, "PYTHONPATH": str(tmp_path / "src")}
    result = lint_imports(tmp_path, env)
    assert result.returncode == 1
    assert "foodgo.ordering is not allowed to import foodgo.app" in result.stdout
