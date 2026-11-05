"""第 4 集：廚房 consumer——至少一次送達＋冪等消費者：同一則訊息收到兩次、處理到一半當掉，都只做一次菜。

執行：
  uv run pytest tests/ep04 -q                  # 不開 Docker：整合測試會 skipped
  docker compose up -d && uv run pytest tests/ep04 -q   # 全部跑
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foodgo import contracts
from foodgo.infra import kafka, outbox
from foodgo.kitchen.consumer import handle
from foodgo.kitchen.repository import InMemoryTicketRepository
from foodgo.kitchen.service import Kitchen, KitchenDown
from foodgo.kitchen.ticket import Dish, Ticket

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


def a001_placed() -> contracts.OrderPlaced:
    return contracts.OrderPlaced(
        order_id="A001",
        restaurant_id="R007",
        items=(contracts.OrderItem(name="牛肉麵", quantity=1), contracts.OrderItem(name="紅茶", quantity=1)),
        note="不要香菜",
    )


class FakeMessage:
    """長得像 confluent_kafka.Message 的假訊息：單元測試不用開 Kafka。"""

    def __init__(self, event: contracts.IntegrationEvent, offset: int = 0) -> None:
        self._value = event.to_json().encode()
        self._offset = offset

    def value(self) -> bytes:
        return self._value

    def key(self) -> bytes:
        return b"A001"

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return self._offset


# ---------------------------------------------------------------- 不需要 Docker：冪等的規則
def test_the_same_message_twice_cooks_once(capsys):
    kitchen = Kitchen()
    event = a001_placed()
    kitchen.on_order_placed(event)
    kitchen.on_order_placed(contracts.OrderPlaced.from_json(event.to_json()))  # 同一則，又送來一次

    assert [(t.ticket_id, t.order_id) for t in kitchen.tickets()] == [("T001", "A001")]
    assert "處理過了" in capsys.readouterr().out


def test_a_new_order_is_a_new_message():
    kitchen = Kitchen()
    kitchen.on_order_placed(a001_placed())
    kitchen.on_order_placed(a001_placed())  # 內容一樣，但 event_id 不同：是另一件事
    assert [t.ticket_id for t in kitchen.tickets()] == ["T001", "T002"]


def test_crash_halfway_then_redelivered_cooks_once():
    tickets = InMemoryTicketRepository()
    event = a001_placed()
    with pytest.raises(KitchenDown):
        Kitchen(tickets=tickets, fault="crash").on_order_placed(event)  # 當掉：什麼都沒記下
    assert tickets.all() == []

    restarted = Kitchen(tickets=tickets)  # 修好、重啟：Kafka 把同一則再送一次
    restarted.on_order_placed(event)
    restarted.on_order_placed(event)  # 開完單、還沒提交位置就又當了一次：再送一次
    assert [t.order_id for t in tickets.all()] == ["A001"]


def test_add_is_the_real_guard():
    tickets = InMemoryTicketRepository()
    assert tickets.add(Ticket("T001", "A001", "R007", (Dish("牛肉麵"),)), message_id="m-1") is True
    assert tickets.add(Ticket("T002", "A001", "R007", (Dish("牛肉麵"),)), message_id="m-1") is False
    assert [t.ticket_id for t in tickets.all()] == ["T001"]


def test_consumer_opens_a_ticket_for_order_placed(capsys):
    kitchen = Kitchen()
    handle(kitchen, FakeMessage(a001_placed(), offset=7))
    [ticket] = kitchen.tickets()
    assert [(d.name, d.quantity) for d in ticket.dishes] == [("牛肉麵", 1), ("紅茶", 1)]
    assert "收到 OrderPlaced A001（partition 0，offset 7）" in capsys.readouterr().out


def test_consumer_skips_events_the_kitchen_does_not_care_about():
    kitchen = Kitchen()
    handle(kitchen, FakeMessage(contracts.MealReady(order_id="A001", ticket_id="T001", restaurant_id="R007")))
    assert kitchen.tickets() == []


# ---------------------------------------------------------------- 不需要 Docker：邊界
def lint_imports(cwd, env=None):
    return subprocess.run(["lint-imports"], cwd=cwd, env=env, capture_output=True, text=True, check=False)


def test_infra_rule_holds():
    result = lint_imports(ROOT)
    assert result.returncode == 0, result.stdout
    assert "基礎設施不認識任何上下文 KEPT" in result.stdout


def test_infra_knowing_the_kitchen_is_caught(tmp_path):
    shutil.copytree(ROOT / "src", tmp_path / "src")
    shutil.copy(ROOT / ".importlinter", tmp_path)
    (tmp_path / "src" / "foodgo" / "infra" / "leak.py").write_text(
        "from foodgo.kitchen.ticket import Ticket\n", encoding="utf-8"
    )
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "src")}
    result = lint_imports(tmp_path, env)
    assert result.returncode == 1
    assert "foodgo.infra is not allowed to import foodgo.kitchen" in result.stdout


def test_infra_needs_no_context():
    code = (
        "import sys, foodgo.infra.config, foodgo.infra.db, foodgo.infra.inbox, foodgo.infra.kafka, foodgo.infra.outbox; "
        "print(sorted(m for m in sys.modules if m.startswith('foodgo.') and not m.startswith('foodgo.infra')))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


def test_glossary_explains_reliable_messaging():
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    for term in ["雙寫", "Transactional Outbox", "relay", "partition", "consumer group", "offset", "lag", "至少一次", "冪等", "processed_messages"]:
        assert term in glossary


# ---------------------------------------------------------------- 需要 Docker：真的走 Kafka 與 PostgreSQL
def postgres_kitchen(db_url: str, **kwargs) -> Kitchen:
    from foodgo.kitchen.postgres import PostgresTicketRepository

    return Kitchen(tickets=PostgresTicketRepository(db_url), **kwargs)


def send_twice(topic: str, event: contracts.OrderPlaced) -> None:
    sender = kafka.KafkaSender()
    for _ in range(2):
        sender.send(topic, event.order_id, event.to_json(), {"event_id": event.event_id, "type": "OrderPlaced"})
    sender.flush()


class Crash(Exception):
    """模擬程序當機。"""


@pytest.mark.integration
def test_consumer_group_cooks_once_when_the_message_arrives_twice(db_url, sql, topic, group):
    send_twice(topic, a001_placed())
    kitchen = postgres_kitchen(db_url)
    kafka.consume(topic, group(), lambda m: handle(kitchen, m), stop_when_idle=3)

    assert sql("SELECT ticket_id, order_id, status FROM tickets") == [("T001", "A001", "received")]
    assert sql("SELECT consumer FROM processed_messages") == [("kitchen",)]


@pytest.mark.integration
def test_crash_after_cooking_before_committing_does_not_cook_twice(db_url, sql, topic, group, capsys):
    event = a001_placed()
    sender = kafka.KafkaSender()
    sender.send(topic, "A001", event.to_json(), {})
    sender.flush()
    kitchen, name = postgres_kitchen(db_url), group()

    def cook_then_crash(message):
        handle(kitchen, message)  # 廚房單已經寫進資料庫……
        raise Crash("……還沒提交位置就當機了")

    with pytest.raises(Crash):
        kafka.consume(topic, name, cook_then_crash)
    assert len(sql("SELECT 1 FROM tickets")) == 1

    kafka.consume(topic, name, lambda m: handle(kitchen, m), stop_when_idle=3)  # 重啟：同一則又收到一次
    assert len(sql("SELECT 1 FROM tickets")) == 1
    assert "處理過了" in capsys.readouterr().out


@pytest.mark.integration
def test_kitchen_down_leaves_the_message_in_kafka(db_url, sql, topic, group):
    sender = kafka.KafkaSender()
    sender.send(topic, "A001", a001_placed().to_json(), {})
    sender.flush()
    name = group()

    down = postgres_kitchen(db_url, fault="crash")
    with pytest.raises(KitchenDown):
        kafka.consume(topic, name, lambda m: handle(down, m))
    assert sql("SELECT 1 FROM tickets") == []

    fixed = postgres_kitchen(db_url)  # 修好重啟：訊息還在 Kafka 等著
    kafka.consume(topic, name, lambda m: handle(fixed, m), stop_when_idle=3)
    assert sql("SELECT order_id FROM tickets") == [("A001",)]


@pytest.mark.integration
def test_place_an_order_and_the_kitchen_gets_it_through_kafka(db_url, topic, group):
    from foodgo.app import create_app
    from foodgo.kitchen.postgres import PostgresTicketRepository
    from foodgo.ordering.postgres import PostgresOrderRepository

    client = TestClient(
        create_app(
            orders=PostgresOrderRepository(db_url, topic=topic),
            kitchen=Kitchen(tickets=PostgresTicketRepository(db_url)),
        )
    )
    assert client.post("/orders", json=A001).status_code == 201
    assert client.get("/tickets").json() == []  # 還在 outbox 裡

    outbox.relay_once(db_url, kafka.KafkaSender())  # relay 程序做的事
    kitchen = postgres_kitchen(db_url)
    kafka.consume(topic, group(), lambda m: handle(kitchen, m), stop_when_idle=3)  # 廚房 consumer 做的事

    [ticket] = client.get("/tickets").json()
    assert (ticket["ticket_id"], ticket["order_id"], ticket["note"]) == ("T001", "A001", "不要香菜")
    assert client.post("/tickets/T001/ready").json()["status"] == "ready"
