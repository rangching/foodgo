"""第 9 集：真的走 Kafka 與 PostgreSQL——投影 consumer 聽五條事件 topic、建 order_tracking；剛下單查不到；砍掉重建一模一樣。

執行（這個檔全部要 Docker）：
  docker compose up -d && uv run pytest tests/ep09 -q
用臨時的 topic 與 consumer group（測完刪掉）、資料庫 foodgo_test。
API（步驟 5）與重建（步驟 8）用到時才 import：做到步驟 4 就能先跑 -k saved_position。
"""
import importlib.util
from pathlib import Path

import pytest
from ep09_facts import declined, happy, refund_failed, rejected
from fastapi.testclient import TestClient

from foodgo.infra import kafka, outbox
from foodgo.ordering.event_sourcing import EventSourcedOrderRepository
from foodgo.tracking import consumer
from foodgo.tracking.postgres import PostgresTrackingRepository

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
TOPIC_OF = {  # 誰公告的就送到誰的 topic
    "OrderPlaced": "ordering",
    "PaymentRequested": "ordering",
    "DeliveryRequested": "ordering",
    "OrderCancelled": "ordering",
    "PaymentAuthorized": "payment",
    "PaymentFailed": "payment",
    "PaymentRefunded": "payment",
    "RefundFailed": "payment",
    "TicketAccepted": "kitchen",
    "TicketRejected": "kitchen",
    "TicketCancelled": "kitchen",
    "CourierAssigned": "delivery",
    "SagaEscalated": "saga",
}


def listens(t) -> list[str]:
    return [t.ordering, t.payment, t.kitchen, t.delivery, t.saga]


def publish(send, t, events) -> int:
    for event in events:
        send(getattr(t, TOPIC_OF[type(event).__name__]), event)
    return len(events)


def project_all(db_url, t, group) -> PostgresTrackingRepository:
    """投影 consumer 跑到閒下來為止（像程序開著、事件都讀完了）。"""
    tracking = PostgresTrackingRepository(db_url)
    consumer.run(tracking, listens=listens(t), group=group(), stop_when_idle=3)
    return tracking


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 投影 consumer：五條 topic → 一張表
def test_the_projector_builds_one_row_per_order(db_url, sql, saga_topics, group, send, capsys):
    t = saga_topics
    sent = publish(send, t, rejected("A001")) + publish(send, t, happy("A002")) + publish(send, t, refund_failed("A003"))
    send(t.payment, happy("A002")[3])  # relay 重送：同一則（同一個 event_id）在 Kafka 上出現兩次、offset 不同
    tracking = project_all(db_url, t, group)

    assert sql("SELECT order_id, status, payment, kitchen, delivery FROM order_tracking ORDER BY order_id") == [
        ("A001", "cancelled", "refunded", "rejected", ""),
        ("A002", "delivering", "authorized", "accepted", "assigned"),
        ("A003", "needs_attention", "refund_failed", "rejected", ""),
    ]
    assert sql("SELECT amount, refunded, cancelled FROM order_tracking WHERE order_id = 'A001'") == [
        (230, 230, "餐廳拒單：餐廳太忙，暫停接單")
    ]
    assert tracking.as_of_position() == sent + 1  # 每一則都記了讀到哪（重送的那一則也算一則）
    [(partitions,)] = sql("SELECT count(*) FROM projection_positions WHERE projection = 'order_tracking'")
    assert 5 <= partitions <= 15  # 一個 partition 一列（5 條 topic × 3 個 partition，有收到訊息的才有）
    out = capsys.readouterr().out
    assert "投影：A002 CourierAssigned → delivering（外送中；" in out

    demo = load("ep09_tracking", ROOT / "demos" / "ep09_tracking.py")
    demo.show("A001", url=db_url)
    shown = capsys.readouterr().out
    for words in ["A001 已取消", "付款  ↺ 已退款 230 元", "廚房  ✕ 餐廳拒單：餐廳太忙，暫停接單"]:
        assert words in shown, words


def test_reading_again_from_the_saved_position(db_url, sql, saga_topics, group, send, capsys):
    """位置記在 projection_positions：換一個 consumer group（Kafka 那邊什麼都沒記）也只讀新的那一則。"""
    t = saga_topics
    publish(send, t, happy("A001"))
    project_all(db_url, t, group)
    publish(send, t, declined("A002")[:1])
    capsys.readouterr()
    project_all(db_url, t, group)  # group() 每次給新的名字
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("投影：")]
    assert lines == [lines[0]] and lines[0].startswith("投影：A002 OrderPlaced → placed")
    assert sql("SELECT order_id, status FROM order_tracking ORDER BY order_id") == [("A001", "delivering"), ("A002", "placed")]


# ---------------------------------------------------------------- 最終一致：剛下單，讀模型還沒有
def test_a_new_order_is_404_until_the_projector_catches_up(db_url, saga_topics, group):
    from foodgo.app import create_app

    t = saga_topics
    tracking = PostgresTrackingRepository(db_url)
    client = TestClient(create_app(orders=EventSourcedOrderRepository(db_url, topic=t.ordering), tracking=tracking))
    assert client.post("/orders", json=A001).status_code == 201  # 寫入端：events＋outbox，已經 commit 了
    assert client.get("/orders/A001").json()["status"] == "placed"  # 寫模型馬上查得到

    first = client.get("/orders/A001/tracking")
    assert first.status_code == 404 and first.json()["as_of_position"] == 0  # 讀模型還沒有：relay 還沒送

    outbox.relay_once(db_url, kafka.KafkaSender())  # relay 送到 Kafka 了……
    assert client.get("/orders/A001/tracking").status_code == 404  # ……投影還沒讀到

    project_all(db_url, t, group)
    body = client.get("/orders/A001/tracking").json()
    assert (body["status"], body["amount"], body["delivery_address"]) == ("placed", 230, "信義路五段 7 號")
    assert body["as_of_position"] == 3  # OrderPlaced、PaymentRequested、DeliveryRequested


# ---------------------------------------------------------------- 砍掉讀模型、從事件重建
def test_rebuilding_gives_exactly_the_same_table(db_url, sql, saga_topics, group, send):
    from foodgo.tracking.rebuild import rebuild

    t = saga_topics
    sent = sum(publish(send, t, scenario(f"A00{n}")) for n, scenario in enumerate([happy, declined, rejected, refund_failed], 1))
    tracking = project_all(db_url, t, group)
    good = tracking.fingerprint()
    assert good[0] == 4

    result = rebuild(tracking, listens=listens(t))
    assert result.before == result.after == good and result.same
    assert result.cleared[0] == 4 and result.replayed == sent  # 清掉 4 列；每一則都重播了一次

    sql("UPDATE order_tracking SET status = 'placed', courier_id = '' WHERE order_id = 'A001' RETURNING order_id")  # 改壞了
    assert tracking.fingerprint() != good
    assert rebuild(tracking, listens=listens(t)).after == good  # 砍掉重建：事件還在，就救得回來


# ---------------------------------------------------------------- 整條流程：編排器＋四個參與者，再投影
def test_a_rejected_order_end_to_end(db_url, saga_topics, group):
    from foodgo.app import create_app

    ep08 = load("ep08_kafka", ROOT / "tests" / "ep08" / "test_ep08_kafka.py")
    t = saga_topics
    ep08.place_a001(db_url, t)
    ep08.Cluster(db_url, t, group).run(kitchen_fault="reject")
    body = TestClient(create_app(tracking=project_all(db_url, t, group))).get("/orders/A001/tracking").json()
    assert (body["status"], body["refunded"], body["cancelled"]) == ("cancelled", 230, "餐廳拒單：餐廳太忙，暫停接單")
    assert [s["state"] for s in body["steps"]] == ["undone", "undone", "failed", "skipped"]


def test_a_refund_that_keeps_failing_shows_needs_attention(db_url, saga_topics, group, messages_in):
    from foodgo.app import create_app

    ep08 = load("ep08_kafka", ROOT / "tests" / "ep08" / "test_ep08_kafka.py")
    t = saga_topics
    ep08.place_a001(db_url, t)
    ep08.Cluster(db_url, t, group).run(kitchen_fault="reject", refund_fault="fail")
    assert ep08.types(messages_in(t.saga)) == ["SagaEscalated"]  # 編排器只公告這一則
    body = TestClient(create_app(tracking=project_all(db_url, t, group))).get("/orders/A001/tracking").json()
    assert (body["status"], body["status_label"]) == ("needs_attention", "人工處理中")
    assert body["attention"] == "RefundPayment 試了 2 次都失敗（發卡銀行暫時無法退款）"
