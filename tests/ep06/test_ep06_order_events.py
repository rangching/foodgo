"""第 6 集：Order 的每個動作都發出事件、由事件算回來；事件 ↔ events 表的 type＋data（codec）。

執行：uv run pytest tests/ep06 -q（這個檔不用 Docker）
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foodgo.app import create_app
from foodgo.ordering import codec
from foodgo.ordering.events import ItemAdded, OrderCancelled, OrderDrafted, OrderPlaced
from foodgo.ordering.fold import fold
from foodgo.ordering.model import Address, Money, Order, OrderRuleViolation, OrderStatus
from foodgo.ordering.repository import InMemoryOrderRepository
from foodgo.shared.event_store import ConcurrencyConflict
from foodgo.shared.events import DomainEvent

ROOT = Path(__file__).resolve().parents[2]

A001 = {
    "customer_id": "C042",
    "restaurant_id": "R007",
    "lines": [
        {"name": "牛肉麵", "unit_price": 160, "quantity": 1},
        {"name": "紅茶", "unit_price": 40, "quantity": 1},
    ],
    "delivery_fee": 30,
    "note": "不要香菜",
    "delivery_address": "信義路五段 7 號",
}


def a001() -> Order:
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


# ---------------------------------------------------------------- Order 的每個動作都是一個事件
def test_every_action_records_an_event():
    order = a001()
    order.place()
    order.cancel("顧客改變心意")
    assert [type(e) for e in order.pull_events()] == [OrderDrafted, ItemAdded, ItemAdded, OrderPlaced, OrderCancelled]


def test_the_new_events_carry_what_was_given():
    drafted, beef, tea = a001().pull_events()
    assert (drafted.order_id, drafted.customer_id, drafted.restaurant_id) == ("A001", "C042", "R007")
    assert (drafted.delivery_address, drafted.delivery_fee, drafted.note) == (Address("信義路五段 7 號"), Money(30), "不要香菜")
    assert (beef.order_id, beef.name, beef.unit_price, beef.quantity) == ("A001", "牛肉麵", Money(160), 1)
    assert (tea.name, tea.unit_price) == ("紅茶", Money(40))


def test_the_version_counts_every_event():
    order = a001()
    assert order.version == 3  # 建立草稿＋兩個品項
    order.place()
    assert order.version == 4
    order.pull_events()
    assert order.version == 4  # 取走事件不會改版本：版本是「套用了幾個事件」


def test_a_broken_rule_records_nothing_and_keeps_the_version():
    order = a001()
    order.place()
    order.pull_events()
    with pytest.raises(OrderRuleViolation):
        order.add_line("滷蛋", Money(15))
    assert order.pull_events() == [] and order.version == 4


def test_folding_the_orders_own_events_gives_the_order_back():
    order = a001()
    order.place()
    order.cancel("顧客改變心意")
    state = fold(order.pull_events())
    for field in ["order_id", "customer_id", "restaurant_id", "delivery_address", "delivery_fee", "note", "lines", "status", "total", "version"]:
        assert getattr(state, field) == getattr(order, field), field


def test_from_state_puts_the_order_back_at_its_version():
    placed = a001()
    placed.place()
    order = Order.from_state(fold(placed.pull_events()))
    assert (order.status, order.total, order.version) == (OrderStatus.PLACED, Money(230), 4)
    assert order.pull_events() == []  # 事情早就發生過了，不再記一次
    order.cancel("顧客改變心意")  # 擺回來之後，規則照樣守、事件照樣記
    assert [type(e) for e in order.pull_events()] == [OrderCancelled] and order.version == 5


def test_restore_is_replaced_by_fold():
    assert not hasattr(Order, "restore")  # 第 4 集的暫時入口：第 6 集起一律 Order.from_state(fold(events))


def test_fold_can_carry_on_from_a_snapshot(a001_afternoon):
    snapshot = fold(a001_afternoon[:4])  # 存下來的第 4 版
    assert fold(a001_afternoon[4:], snapshot) == fold(a001_afternoon)  # 從快照接著套，和從頭算一樣
    assert fold([], snapshot) == snapshot


# ---------------------------------------------------------------- codec：事件 ↔ type＋data
def test_an_event_becomes_a_type_and_plain_json(a001_afternoon):
    drafted, beef, tea, placed, cancelled = (codec.to_data(e) for e in a001_afternoon)
    assert drafted == (
        "OrderDrafted",
        {
            "order_id": "A001",
            "customer_id": "C042",
            "restaurant_id": "R007",
            "delivery_address": "信義路五段 7 號",
            "delivery_fee": 30,
            "note": "不要香菜",
        },
    )
    assert beef == ("ItemAdded", {"order_id": "A001", "name": "牛肉麵", "unit_price": 160, "quantity": 1})
    assert placed[1]["lines"] == [
        {"name": "牛肉麵", "unit_price": 160, "quantity": 1},
        {"name": "紅茶", "unit_price": 40, "quantity": 1},
    ]
    assert cancelled == ("OrderCancelled", {"order_id": "A001", "reason": "顧客改變心意"})
    for _, data in (drafted, beef, tea, placed, cancelled):
        assert "event_id" not in data and "occurred_at" not in data  # 這兩個是 events 表自己的欄位
        json.dumps(data)  # 只有 JSON 認得的型別


def test_codec_round_trip_keeps_every_field(a001_afternoon):
    for event in a001_afternoon:
        kind, data = codec.to_data(event)
        back = codec.from_data(kind, json.loads(json.dumps(data)), event_id=event.event_id, occurred_at=event.occurred_at)
        assert back == event and type(back) is type(event)


def test_codec_knows_every_ordering_event():
    module = importlib.import_module("foodgo.ordering.events")
    found = {
        name for name, obj in vars(module).items()
        if isinstance(obj, type) and issubclass(obj, DomainEvent) and obj is not DomainEvent
    }
    assert set(codec.EVENT_TYPES) == found, "新的點餐事件要加進 codec：不然存得進去、讀不回來"


def test_codec_refuses_what_it_does_not_know(a001_afternoon):
    event = a001_afternoon[0]
    with pytest.raises(TypeError, match="SomethingHappened"):
        codec.from_data("SomethingHappened", {}, event_id=event.event_id, occurred_at=event.occurred_at)


def test_a_state_round_trips_for_snapshots(a001_afternoon):
    for n in range(1, 6):
        state = fold(a001_afternoon[:n])
        data = codec.state_to_data(state)
        assert codec.state_from_data(json.loads(json.dumps(data))) == state
    data = codec.state_to_data(fold(a001_afternoon[:4]))
    assert (data["status"], data["version"], data["delivery_fee"]) == ("placed", 4, 30)


# ---------------------------------------------------------------- 邊界：infra 只認 type＋data
def test_the_event_store_in_infra_knows_no_context():
    code = (
        "import sys, foodgo.infra.event_store; "
        "print(sorted(m for m in sys.modules if m.startswith('foodgo.') and not m.startswith('foodgo.infra')))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


def test_the_events_table_is_append_only_with_one_version_each():
    from foodgo.infra import event_store

    schema = " ".join(event_store.SCHEMA.split())
    for piece in ["CREATE TABLE IF NOT EXISTS events", "position bigserial", "data jsonb", "occurred_at timestamptz",
                  "UNIQUE (stream_id, version)", "CREATE TABLE IF NOT EXISTS snapshots", "state jsonb"]:
        assert piece in schema, piece
    assert "BEFORE UPDATE OR DELETE OR TRUNCATE ON events" in schema  # 資料庫自己擋：不能改、不能刪


def lint_imports(cwd, env=None):
    return subprocess.run(["lint-imports"], cwd=cwd, env=env, capture_output=True, text=True, check=False)


def test_boundary_rules_hold():
    result = lint_imports(ROOT)
    assert result.returncode == 0, result.stdout
    assert "基礎設施不認識任何上下文 KEPT" in result.stdout
    assert "點餐的領域模型不碰基礎設施 KEPT" in result.stdout


def test_the_codec_touching_the_database_is_caught(tmp_path):
    shutil.copytree(ROOT / "src", tmp_path / "src")
    shutil.copy(ROOT / ".importlinter", tmp_path)
    codec_file = tmp_path / "src" / "foodgo" / "ordering" / "codec.py"
    codec_file.write_text(codec_file.read_text(encoding="utf-8") + "\nfrom foodgo.infra import event_store\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "src")}
    result = lint_imports(tmp_path, env)
    assert result.returncode == 1
    assert "foodgo.ordering.codec is not allowed to import foodgo.infra" in result.stdout


# ---------------------------------------------------------------- API：有人先寫了 → 409
class SomeoneWroteFirst(InMemoryOrderRepository):
    """存的時候，事件儲存說「有人先寫了」（不用資料庫，假裝一下）。"""

    racing = False

    def save(self, order: Order) -> None:
        if self.racing:
            order.pull_events()
            raise ConcurrencyConflict(order.order_id, 4, 5)
        super().save(order)


def test_the_api_answers_409_when_someone_wrote_first():
    orders = SomeoneWroteFirst()
    client = TestClient(create_app(orders=orders))
    assert client.post("/orders", json=A001).status_code == 201
    orders.racing = True
    response = client.post("/orders/A001/cancel", json={"reason": "顧客改變心意"})
    assert response.status_code == 409
    assert "有人先寫了" in response.json()["detail"]


def test_the_api_shows_the_version():
    client = TestClient(create_app())
    assert client.post("/orders", json=A001).json()["version"] == 4
    assert client.post("/orders/A001/cancel", json={"reason": "顧客改變心意"}).json()["version"] == 5
    assert client.get("/orders/A001").json()["version"] == 5


def test_the_time_machine_needs_an_event_store():
    client = TestClient(create_app())  # 記憶體版 Repository 只記現在的樣子
    client.post("/orders", json=A001)
    assert client.get("/orders/A001", params={"as_of": "2026-11-06T15:00:00"}).status_code == 501
    assert client.get("/orders/A001/events").status_code == 501
    paths = client.get("/openapi.json").json()["paths"]
    assert "/orders/{order_id}/events" in paths
    assert "as_of" in [p["name"] for p in paths["/orders/{order_id}"]["get"]["parameters"]]


# ---------------------------------------------------------------- 詞彙表
@pytest.mark.parametrize(
    "term",
    ["`events`", "`snapshots`", "position", "UNIQUE (stream_id, version)", "codec", "Order.from_state", "快照", "時光機", "load_as_of", "409", "重播"],
)
def test_glossary_explains_event_sourcing_in_postgres(term):
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    assert term in glossary
