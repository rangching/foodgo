"""第 4 集：點餐把訂單存進 PostgreSQL，事件寫進 outbox（同一個交易），relay 再送到 Kafka。

執行：
  uv run pytest tests/ep04 -q                  # 不開 Docker：整合測試會 skipped
  docker compose up -d && uv run pytest tests/ep04 -q   # 全部跑
"""
import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from foodgo import contracts
from foodgo.app import create_app
from foodgo.infra import outbox
from foodgo.infra.kafka import KafkaSender
from foodgo.ordering.events import OrderCancelled, OrderPlaced
from foodgo.ordering.integration import to_integration
from foodgo.ordering.model import (
    Address,
    Money,
    Order,
    OrderLine,
    OrderRuleViolation,
    OrderStatus,
)
from foodgo.ordering.postgres import UPSERT, PostgresOrderRepository, row_of
from foodgo.ordering.repository import InMemoryOrderRepository

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


def a001(order_id: str = "A001") -> Order:
    order = Order(
        order_id=order_id,
        customer_id="C042",
        restaurant_id="R007",
        delivery_address=Address("信義路五段 7 號"),
        delivery_fee=Money(30),
        note="不要香菜",
    )
    order.add_line("牛肉麵", Money(160))
    order.add_line("紅茶", Money(40))
    return order


# ---------------------------------------------------------------- 不需要 Docker
def test_saving_also_hands_out_the_events():
    published = []
    repo = InMemoryOrderRepository(published.append)
    order = a001()
    order.place()
    repo.save(order)
    assert [type(e) for e in published] == [OrderPlaced]
    assert order.pull_events() == []  # Repository 已經取走了

    order.cancel("顧客改變心意")
    repo.save(order)
    assert [type(e) for e in published] == [OrderPlaced, OrderCancelled]


def test_restore_puts_an_order_back_without_new_events():
    order = Order.restore(
        order_id="A001",
        customer_id="C042",
        restaurant_id="R007",
        delivery_address=Address("信義路五段 7 號"),
        delivery_fee=Money(30),
        note="不要香菜",
        lines=[OrderLine("牛肉麵", Money(160)), OrderLine("紅茶", Money(40))],
        status=OrderStatus.PLACED,
    )
    assert order.total == Money(230)
    assert order.pull_events() == []  # 事情早就發生過了，不再記一次
    with pytest.raises(OrderRuleViolation):
        order.add_line("滷蛋", Money(15))  # 擺回來之後，規則照樣守


def test_ordering_announces_on_one_topic_keyed_by_order():
    assert contracts.ORDERING_TOPIC == "ordering.events"
    message = outbox.OutboxMessage(1, "e-1", "ordering.events", "A001", "OrderPlaced", "{}")
    assert message.headers == {"event_id": "e-1", "type": "OrderPlaced"}


# ---------------------------------------------------------------- 需要 Docker
@pytest.mark.integration
def test_postgres_repository_round_trip(db_url):
    repo = PostgresOrderRepository(db_url)
    assert [repo.next_id(), repo.next_id()] == ["A001", "A002"]
    order = a001()
    order.place()
    repo.save(order)

    again = PostgresOrderRepository(db_url).get("A001")  # 另一個 Repository：真的是從資料庫讀回來的
    assert again is not order and again == order
    assert (again.status, again.total, again.note) == (OrderStatus.PLACED, Money(230), "不要香菜")
    assert [(line.name, line.unit_price, line.quantity) for line in again.lines] == [
        ("牛肉麵", Money(160), 1),
        ("紅茶", Money(40), 1),
    ]
    assert repo.get("A999") is None


@pytest.mark.integration
def test_order_and_outbox_row_are_written_together(db_url, sql, topic):
    repo = PostgresOrderRepository(db_url, topic=topic)
    order = a001()
    order.place()
    repo.save(order)

    assert sql("SELECT order_id, status FROM orders") == [("A001", "placed")]
    [(event_id, row_topic, key, kind, payload, sent_at)] = sql(
        "SELECT event_id, topic, key, type, payload::text, sent_at FROM outbox"
    )
    assert (row_topic, key, kind, sent_at) == (topic, "A001", "OrderPlaced", None)  # 還沒送
    envelope = json.loads(payload)  # 整合事件的 JSON 信封，原封不動
    assert (envelope["type"], envelope["event_id"]) == ("OrderPlaced", event_id)
    assert envelope["data"] == {
        "order_id": "A001",
        "restaurant_id": "R007",
        "items": [{"name": "牛肉麵", "quantity": 1}, {"name": "紅茶", "quantity": 1}],
        "note": "不要香菜",
    }

    order.cancel("顧客改變心意")
    repo.save(order)
    assert sql("SELECT status FROM orders") == [("cancelled",)]
    assert len(sql("SELECT 1 FROM outbox")) == 1  # OrderCancelled 不對外公告，不進 outbox


@pytest.mark.integration
def test_if_the_outbox_write_fails_the_order_is_not_saved_either(db_url, sql, monkeypatch):
    def broken_put(*args, **kwargs):
        raise RuntimeError("寫 outbox 時當機")

    monkeypatch.setattr(outbox, "put", broken_put)
    order = a001()
    order.place()
    with pytest.raises(RuntimeError):
        PostgresOrderRepository(db_url).save(order)
    assert sql("SELECT 1 FROM orders") == []  # 同一個交易：一起失敗


class DualWriteOrderRepository(PostgresOrderRepository):
    """對照組（步驟 3、4 的直覺版）：先 commit 訂單，再直接送 Kafka。兩件事中間當掉，事件就不見了。"""

    def __init__(self, url, *, topic, crash_between):
        super().__init__(url, topic=topic)
        self._sender = KafkaSender()
        self._crash_between = crash_between

    def save(self, order):
        with psycopg.connect(self._url) as conn:
            conn.execute(UPSERT, row_of(order))
        if self._crash_between:
            raise RuntimeError("點餐當機：訂單已經存進資料庫，事件還沒送出去")
        for event in order.pull_events():
            message = to_integration(event)
            if message is not None:
                self._sender.send(self._topic, order.order_id, message.to_json(), {})
                self._sender.flush()


@pytest.mark.integration
def test_dual_write_loses_the_event_when_it_crashes_in_between(db_url, topic, read_topic):
    fine = TestClient(create_app(orders=DualWriteOrderRepository(db_url, topic=topic, crash_between=False)))
    assert fine.post("/orders", json=A001).status_code == 201

    orders = DualWriteOrderRepository(db_url, topic=topic, crash_between=True)
    crashing = TestClient(create_app(orders=orders), raise_server_exceptions=False)
    assert crashing.post("/orders", json=A001).status_code == 500  # 顧客以為沒下成功
    assert crashing.get("/orders/A002").json()["status"] == "placed"  # 資料庫裡卻有這張訂單

    assert [m.key() for m in read_topic(topic)] == [b"A001"]  # Kafka 上只有 A001：A002 的事件不見了


@pytest.mark.integration
def test_relay_sends_the_outbox_to_kafka(db_url, sql, topic, read_topic):
    order = a001()
    order.place()
    PostgresOrderRepository(db_url, topic=topic).save(order)

    sent = outbox.relay_once(db_url, KafkaSender())
    assert [(m.type, m.key) for m in sent] == [("OrderPlaced", "A001")]

    [message] = read_topic(topic)
    assert message.key() == b"A001"  # key＝order_id：同一張訂單永遠進同一個 partition
    headers = {name: value.decode() for name, value in message.headers()}
    assert headers == {"event_id": sent[0].event_id, "type": "OrderPlaced"}
    envelope = json.loads(message.value())
    assert envelope["event_id"] == sent[0].event_id
    assert envelope["data"]["items"] == [{"name": "牛肉麵", "quantity": 1}, {"name": "紅茶", "quantity": 1}]

    assert sql("SELECT count(*) FROM outbox WHERE sent_at IS NOT NULL") == [(1,)]
    assert outbox.relay_once(db_url, KafkaSender()) == []  # 送過的不再送


@pytest.mark.integration
def test_relay_crash_after_sending_means_the_event_is_sent_twice(db_url, sql, topic, read_topic):
    order = a001()
    order.place()
    PostgresOrderRepository(db_url, topic=topic).save(order)

    with pytest.raises(outbox.RelayCrashed):
        outbox.relay_once(db_url, KafkaSender(), fault="crash-after-send")
    assert sql("SELECT count(*) FROM outbox WHERE sent_at IS NULL") == [(1,)]  # 沒來得及標記

    outbox.relay_once(db_url, KafkaSender())  # 重啟後再送一次
    first, second = read_topic(topic)
    assert first.value() == second.value()  # 同一則事件，Kafka 上有兩份：至少一次（at-least-once）
    assert sql("SELECT count(*) FROM outbox WHERE sent_at IS NULL") == [(0,)]


@pytest.mark.integration
def test_the_api_keeps_orders_in_postgres(db_url):
    from foodgo.app import create_postgres_app

    with TestClient(create_postgres_app(db_url)) as client:  # with：會跑啟動時的建表
        response = client.post("/orders", json=A001)
        assert (response.status_code, response.json()["total"]) == (201, 230)
    with TestClient(create_postgres_app(db_url)) as restarted:  # 重開一次 API，訂單還在
        assert restarted.get("/orders/A001").json()["status"] == "placed"
        assert restarted.post("/orders/A001/cancel", json={"reason": "顧客改變心意"}).status_code == 200
        assert restarted.post("/orders/A001/cancel", json={"reason": "再取消一次"}).status_code == 409
