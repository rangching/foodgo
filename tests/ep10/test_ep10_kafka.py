"""第 10 集：真的走 PostgreSQL 與 Kafka——每個服務自己的資料庫；trace context 跟著 outbox、Kafka header 一路傳下去。

執行（這個檔全部要 Docker）：
  docker compose up -d && uv run pytest tests/ep10 -q
用資料庫 foodgo_test 與臨時的 topic、consumer group（測完刪掉）；span 收在記憶體裡，不送 Jaeger。
"""
import threading
import time

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from foodgo import contracts
from foodgo.infra import db, kafka, outbox, telemetry

pytestmark = pytest.mark.integration

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
PLACED = contracts.OrderPlaced(
    order_id="A001",
    restaurant_id="R007",
    items=(contracts.OrderItem(name="牛肉麵", quantity=1), contracts.OrderItem(name="紅茶", quantity=1)),
    note="不要香菜",
)


def trace_of(span) -> str:
    return format(span.context.trace_id, "032x")


def headers_of(message) -> dict[str, str]:
    return {name: value.decode() for name, value in message.headers()}


# ---------------------------------------------------------------- 每個服務自己的資料庫
def test_a_service_creates_its_own_database(test_db_url):
    url = test_db_url.rsplit("/", 1)[0] + "/foodgo_test_ensure"
    admin = test_db_url.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS foodgo_test_ensure")
    try:
        assert db.ensure_database(url) is True  # 第一次：建
        assert db.ensure_database(url) is False  # 第二次：已經有了
        from foodgo.payment.postgres import SCHEMAS

        db.create_tables(url, *SCHEMAS)
        with psycopg.connect(url) as conn:
            names = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        assert names == {"payments", "payment_outbox", "processed_messages"}  # 付款的資料庫裡只有付款的表
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute("DROP DATABASE IF EXISTS foodgo_test_ensure WITH (FORCE)")


# ---------------------------------------------------------------- outbox 抄下 trace context
def test_the_outbox_keeps_the_trace_context(db_url, sql, spans):
    with psycopg.connect(db_url) as conn:
        outbox.put(conn, topic="t", key="A001", message=PLACED)  # 沒在追蹤：第 4～9 集的樣子
    with telemetry.tracer.start_as_current_span("POST /orders"), telemetry.correlated("c-42"):
        trace_id = telemetry.current_trace_id()
        with psycopg.connect(db_url) as conn:
            outbox.put(conn, topic="t", key="A001", message=contracts.MealReady(order_id="A001", ticket_id="T001", restaurant_id="R007"))
    rows = sql("SELECT type, headers FROM outbox ORDER BY id")
    assert rows[0] == ("OrderPlaced", {})
    kind, headers = rows[1]
    assert kind == "MealReady"
    assert headers["correlation_id"] == "c-42"
    assert headers["traceparent"].split("-")[1] == trace_id


def test_relay_puts_traceparent_and_correlation_id_on_the_kafka_message(db_url, topic, read_topic, spans):
    with telemetry.tracer.start_as_current_span("POST /orders"), telemetry.correlated("c-42"), psycopg.connect(db_url) as conn:
        outbox.put(conn, topic=topic, key="A001", message=PLACED)
    [sent] = outbox.relay_once(db_url, kafka.KafkaSender())
    request, send = spans()
    assert send.name == "send OrderPlaced" and send.parent.span_id == request.context.span_id
    [message] = read_topic(topic)
    headers = headers_of(message)
    assert headers["event_id"] == sent.event_id and headers["type"] == "OrderPlaced"  # 第 4 集的兩個照舊
    assert headers["correlation_id"] == "c-42"
    assert headers["traceparent"].split("-")[1:3] == [trace_of(send), format(send.context.span_id, "016x")]


def test_a_consumer_picks_up_the_trace_and_passes_it_on(db_url, sql, topic, group, spans):
    with telemetry.tracer.start_as_current_span("POST /orders"), telemetry.correlated("c-42"), psycopg.connect(db_url) as conn:
        outbox.put(conn, topic=topic, key="A001", message=PLACED)
    outbox.relay_once(db_url, kafka.KafkaSender())

    def handle(message) -> None:  # 像廚房：收到一則、處理時又寫一則進自己的 outbox
        with psycopg.connect(db_url) as conn:
            outbox.put(conn, topic=topic, key="A001", message=contracts.MealReady(order_id="A001", ticket_id="T001", restaurant_id="R007"))

    kafka.consume(topic, group(), handle, stop_when_idle=2)
    request, send, process = spans()
    assert process.name.endswith("process OrderPlaced")
    assert len({trace_of(request), trace_of(send), trace_of(process)}) == 1  # 一條 trace
    assert process.parent.span_id == send.context.span_id
    assert process.attributes["foodgo.correlation_id"] == "c-42"
    [(headers,)] = sql("SELECT headers FROM outbox WHERE type = 'MealReady'")
    assert headers["traceparent"].split("-")[2] == format(process.context.span_id, "016x")  # 下一段接在 process 後面
    assert headers["correlation_id"] == "c-42"


def test_a_consumer_stops_when_asked(topic, group):
    stop = threading.Event()
    worker = threading.Thread(target=kafka.consume, args=(topic, group(), lambda m: None), kwargs={"stop": stop})
    worker.start()
    time.sleep(3)
    began = time.monotonic()
    stop.set()  # 服務要關了
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert time.monotonic() - began < 3  # 最多等一次 poll（1 秒）


# ---------------------------------------------------------------- Gateway → 點餐服務 → outbox：同一條 trace、同一個關聯 ID
def test_an_order_through_the_gateway_carries_one_trace_into_the_outbox(db_url, sql, spans):
    from foodgo.services import gateway, ordering

    service = ordering.create_app(db_url)  # 沒有進 lifespan：背景工作沒起來（不會去聽正式的 topic）
    front = gateway.create_app({"ordering": "http://ordering"}, transport=httpx.ASGITransport(app=service))
    response = TestClient(front).post("/orders", json=A001)
    assert response.status_code == 201
    assert response.json()["order_id"] == "A001"
    cid, trace_id = response.headers["x-correlation-id"], response.headers["x-trace-id"]

    names = sorted((s.name, trace_of(s)) for s in spans())
    # Gateway 的 server span、轉出去的 client span、點餐服務的 server span：同一條 trace
    assert names == [("POST /orders", trace_id), ("POST /orders", trace_id), ("POST → ordering", trace_id)]
    rows = sql("SELECT type, headers FROM outbox ORDER BY id")
    assert [kind for kind, _ in rows] == ["OrderPlaced", "PaymentRequested", "DeliveryRequested"]
    for _, headers in rows:  # relay 送出時就從這裡接下去
        assert headers["correlation_id"] == cid
        assert headers["traceparent"].split("-")[1] == trace_id

    again = TestClient(front).get("/orders/A001", headers={"X-Correlation-ID": cid})
    assert (again.status_code, again.json()["total"], again.headers["x-correlation-id"]) == (200, 230, cid)
