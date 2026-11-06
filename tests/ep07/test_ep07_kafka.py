"""第 7 集：真的走 Kafka 與 PostgreSQL——A001 從下單接力到外送員指派；付款失敗時停在半路；「走到哪」要拼四個地方。

執行（這個檔全部要 Docker）：
  docker compose up -d && uv run pytest tests/ep07 -q
用臨時的 topic 與 consumer group（測完刪掉），不會動到 Kafka UI 上正式的 ordering.events…。
"""
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foodgo import contracts
from foodgo.app import create_app
from foodgo.delivery import consumer as delivery_consumer
from foodgo.delivery.postgres import PostgresDeliveryRepository
from foodgo.delivery.service import Dispatch
from foodgo.infra import kafka, outbox
from foodgo.kitchen import consumer as kitchen_consumer
from foodgo.kitchen.postgres import PostgresTicketRepository
from foodgo.kitchen.service import Kitchen
from foodgo.ordering.event_sourcing import EventSourcedOrderRepository
from foodgo.payment import consumer as payment_consumer
from foodgo.payment.fakepay import FakePay
from foodgo.payment.gateway import FakePayAdapter
from foodgo.payment.postgres import PostgresPaymentRepository
from foodgo.payment.service import Payments

pytestmark = pytest.mark.integration

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


def place_a001(db_url: str, topics) -> None:
    """下單（API）＋ relay 程序做的事：三則整合事件到點餐的 topic。"""
    client = TestClient(create_app(orders=EventSourcedOrderRepository(db_url, topic=topics.ordering)))
    assert client.post("/orders", json=A001).status_code == 201
    outbox.relay_once(db_url, kafka.KafkaSender())


def run_payment(db_url, topics, group, fault: str = "") -> None:
    payments = Payments(FakePayAdapter(FakePay(fault=fault)), PostgresPaymentRepository(db_url, topic=topics.payment))
    payment_consumer.run(payments, db_url, listens=[topics.ordering], group=group, stop_when_idle=2)


def run_kitchen(db_url, topics, group, listens=None) -> None:
    kitchen = Kitchen(tickets=PostgresTicketRepository(db_url, topic=topics.kitchen))
    kitchen_consumer.run(
        kitchen, db_url, listens=listens or [topics.ordering, topics.payment], group=group, stop_when_idle=2
    )


def run_delivery(db_url, topics, group) -> None:
    dispatch = Dispatch(PostgresDeliveryRepository(db_url, topic=topics.delivery))
    delivery_consumer.run(dispatch, db_url, listens=[topics.ordering, topics.kitchen], group=group, stop_when_idle=2)


def types(messages) -> list[str]:
    return [dict(m.headers())["type"].decode() for m in messages]


def load_demo():
    spec = importlib.util.spec_from_file_location("ep07_choreography", ROOT / "demos" / "ep07_choreography.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 一路走到外送員指派
def test_a001_goes_all_the_way_to_a_courier(db_url, sql, topics, group, messages_in, capsys):
    groups = {"payment": group(), "kitchen": group(), "delivery": group()}
    place_a001(db_url, topics)
    run_payment(db_url, topics, groups["payment"])
    run_kitchen(db_url, topics, groups["kitchen"])
    run_delivery(db_url, topics, groups["delivery"])

    # 各上下文自己的表
    assert sql("SELECT payment_id, order_id, amount, status FROM payments") == [("P001", "A001", 230, "authorized")]
    assert sql("SELECT ticket_id, order_id, status FROM tickets") == [("T001", "A001", "accepted")]
    assert sql("SELECT delivery_id, order_id, pickup, dropoff, status, courier_id FROM deliveries") == [
        ("D001", "A001", "R007", "信義路五段 7 號", "assigned", "M01")
    ]
    for table in ["outbox", "payment_outbox", "kitchen_outbox", "delivery_outbox"]:
        assert sql(f"SELECT count(*) FROM {table} WHERE sent_at IS NULL") == [(0,)], table  # 全部送出去了
    assert sorted(c for (c,) in sql("SELECT DISTINCT consumer FROM processed_messages")) == [
        "delivery",
        "kitchen",
        "payment",
    ]

    # 同一張訂單分在四個 topic：key 都是 A001，所以每個 topic 都落在同一號 partition
    by_topic = {name: messages_in(getattr(topics, name)) for name in ["ordering", "payment", "kitchen", "delivery"]}
    assert {name: types(messages) for name, messages in by_topic.items()} == {
        "ordering": ["OrderPlaced", "PaymentRequested", "DeliveryRequested"],
        "payment": ["PaymentAuthorized"],
        "kitchen": ["TicketAccepted"],
        "delivery": ["CourierAssigned"],
    }
    every = [m for messages in by_topic.values() for m in messages]
    assert {m.key() for m in every} == {b"A001"}
    assert len({m.partition() for m in every}) == 1

    # 「走到哪」：示範腳本的 trace 要問四張表、讀四個 topic
    demo = load_demo()
    capsys.readouterr()
    demo.trace(
        "A001",
        url=db_url,
        topics={"點餐": topics.ordering, "付款": topics.payment, "廚房": topics.kitchen, "外送": topics.delivery},
        groups={
            groups["payment"]: [topics.ordering],
            groups["kitchen"]: [topics.ordering, topics.payment],
            groups["delivery"]: [topics.ordering, topics.kitchen],
        },
    )
    out = capsys.readouterr().out
    for words in [
        "第 4 版 placed",
        "P001 authorized 230 元",
        "T001 accepted",
        "D001 assigned，外送員 M01",
        "6 則，分在 4 個 topic",
        "CourierAssigned",
    ]:
        assert words in out, words
    assert out.count(" 0") >= 3  # 三個 consumer group 的 lag 都是 0


# ---------------------------------------------------------------- 付款失敗：停在半路
def test_a_declined_payment_stops_halfway(db_url, sql, topics, group, messages_in, capsys):
    groups = {"payment": group(), "kitchen": group(), "delivery": group()}
    place_a001(db_url, topics)
    run_payment(db_url, topics, groups["payment"], fault="decline")
    run_kitchen(db_url, topics, groups["kitchen"])
    run_delivery(db_url, topics, groups["delivery"])

    assert sql("SELECT status, reason FROM payments") == [("failed", "卡片額度不足")]
    assert sql("SELECT status FROM tickets") == [("cancelled",)]
    assert sql("SELECT status, courier_id FROM deliveries") == [("waiting", "")]  # 外送還在等
    assert types(messages_in(topics.payment)) == ["PaymentFailed"]
    assert messages_in(topics.kitchen) == [] and messages_in(topics.delivery) == []
    order = EventSourcedOrderRepository(db_url).get("A001")
    assert order.status.value == "placed"  # 沒有人告訴點餐

    demo = load_demo()
    capsys.readouterr()
    demo.trace(
        "A001",
        url=db_url,
        topics={"點餐": topics.ordering, "付款": topics.payment, "廚房": topics.kitchen, "外送": topics.delivery},
        groups={groups["payment"]: [topics.ordering]},
    )
    out = capsys.readouterr().out
    assert "P001 failed 230 元（卡片額度不足）" in out and "T001 cancelled" in out and "D001 waiting" in out
    assert "點餐不知道" in out


# ---------------------------------------------------------------- 冪等與先後
def test_a_request_delivered_twice_is_charged_once(db_url, sql, topics, group):
    request = contracts.PaymentRequested(order_id="A001", customer_id="C042", amount=230)
    sender = kafka.KafkaSender()
    for _ in range(2):
        sender.send(
            topics.ordering, "A001", request.to_json(), {"event_id": request.event_id, "type": "PaymentRequested"}
        )
    sender.flush()
    run_payment(db_url, topics, group())
    assert sql("SELECT count(*) FROM payments") == [(1,)]
    assert sql("SELECT type FROM payment_outbox") == [("PaymentAuthorized",)]


def test_the_kitchen_hears_the_payment_before_the_order(db_url, sql, topics, group):
    """廚房先只聽付款的 topic（OrderPlaced 還沒讀到），之後才讀到點餐的：照樣接單。"""
    order_placed = contracts.OrderPlaced(
        order_id="A001", restaurant_id="R007", items=(contracts.OrderItem(name="牛肉麵", quantity=1),), note="不要香菜"
    )
    authorized = contracts.PaymentAuthorized(order_id="A001", payment_id="P001")
    sender = kafka.KafkaSender()
    sender.send(topics.ordering, "A001", order_placed.to_json(), {"type": "OrderPlaced"})
    sender.send(topics.payment, "A001", authorized.to_json(), {"type": "PaymentAuthorized"})
    sender.flush()

    name = group()
    run_kitchen(db_url, topics, name, listens=[topics.payment])  # 付款的結果先到
    assert sql("SELECT outcome FROM kitchen_payments") == [("authorized",)]
    assert sql("SELECT count(*) FROM tickets") == [(0,)]
    run_kitchen(db_url, topics, name)  # 再讀到 OrderPlaced
    assert sql("SELECT status FROM tickets") == [("accepted",)]
    assert sql("SELECT type, sent_at IS NOT NULL FROM kitchen_outbox") == [("TicketAccepted", True)]


def test_meal_ready_goes_through_the_kitchen_outbox(db_url, sql):
    from foodgo.app import create_postgres_app

    with TestClient(create_postgres_app(db_url)) as client:
        client.post("/orders", json=A001)
        kitchen = Kitchen(tickets=PostgresTicketRepository(db_url))
        kitchen.on_order_placed(
            contracts.OrderPlaced(
                order_id="A001", restaurant_id="R007", items=(contracts.OrderItem(name="牛肉麵", quantity=1),)
            )
        )
        assert client.post("/tickets/T001/ready").json()["status"] == "ready"
    assert sql("SELECT type, topic FROM kitchen_outbox") == [("MealReady", "kitchen.events")]
