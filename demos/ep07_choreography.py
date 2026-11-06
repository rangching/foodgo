"""第 7 集示範：編舞式 Saga——付款、廚房、外送各自聽事件接力，一張訂單從下單一路走到外送員指派。

    uv run python demos/ep07_choreography.py              # 起 relay＋三個 consumer、下一張單、等到外送員指派，再查走到哪
    uv run python demos/ep07_choreography.py decline      # 付款失敗的那一條（付款 consumer 加 FOODGO_FAULT_PAYMENT=decline）
    uv run python demos/ep07_choreography.py trace A001   # 只查：A001 走到哪了（4 張表、4 個 topic、3 個 consumer group）

要先 docker compose up -d。用 API 的資料庫（foodgo）與正式的 topic，跑完在 Kafka UI 看得到。
自己開著的 relay、consumer 先停掉（Ctrl-C）：不然同一個 consumer group 會有兩個成員分著讀，紀錄印在兩個地方。
"""

import os
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import psycopg
from confluent_kafka import OFFSET_BEGINNING, Consumer, TopicPartition
from fastapi.testclient import TestClient

from foodgo import contracts
from foodgo.app import create_postgres_app
from foodgo.delivery.postgres import SCHEMAS as DELIVERY
from foodgo.infra import config, db, kafka
from foodgo.kitchen.postgres import SCHEMAS as KITCHEN
from foodgo.ordering.event_sourcing import SCHEMAS as ORDERING
from foodgo.ordering.event_sourcing import EventSourcedOrderRepository
from foodgo.payment.postgres import SCHEMAS as PAYMENT

TAIPEI = timezone(timedelta(hours=8))
A001 = {
    "customer_id": "C042",
    "restaurant_id": "R007",
    "lines": [{"name": "牛肉麵", "unit_price": 160, "quantity": 1}, {"name": "紅茶", "unit_price": 40, "quantity": 1}],
    "delivery_fee": 30,
    "note": "不要香菜",
    "delivery_address": "信義路五段 7 號",
}
PROGRAMS = {  # 平常是四個終端機各跑一個；這裡一次起好
    "relay": "foodgo.ordering.relay",
    "付款": "foodgo.payment.consumer",
    "廚房": "foodgo.kitchen.consumer",
    "外送": "foodgo.delivery.consumer",
}
TOPICS = {
    "點餐": contracts.ORDERING_TOPIC,
    "付款": contracts.PAYMENT_TOPIC,
    "廚房": contracts.KITCHEN_TOPIC,
    "外送": contracts.DELIVERY_TOPIC,
}
GROUPS = {  # consumer group：誰聽哪些 topic
    "payment": [contracts.ORDERING_TOPIC],
    "kitchen": [contracts.ORDERING_TOPIC, contracts.PAYMENT_TOPIC],
    "delivery": [contracts.ORDERING_TOPIC, contracts.KITCHEN_TOPIC],
}


# ---------------------------------------------------------------- 排版：中文字佔兩格
def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, size: int) -> str:
    return text + " " * (size - width(text))


# ---------------------------------------------------------------- 起程序、下單、等
def start(fault_payment: str = "") -> list[subprocess.Popen]:
    url = config.database_url()
    db.create_tables(url, *ORDERING, *PAYMENT, *KITCHEN, *DELIVERY)  # 先建好，四個程序同時起來也不會撞
    for topic in TOPICS.values():
        kafka.ensure_topic(topic)
    programs = []
    for name, module in PROGRAMS.items():
        env = dict(os.environ)
        if name == "付款" and fault_payment:
            env["FOODGO_FAULT_PAYMENT"] = fault_payment
        programs.append(subprocess.Popen([sys.executable, "-m", module], env=env))
    time.sleep(3)  # 等三個 consumer 加入自己的 consumer group
    return programs


def stop(programs: list[subprocess.Popen]) -> None:
    time.sleep(1.5)  # 讓最後一則送完、位置提交完
    for program in programs:
        program.send_signal(signal.SIGINT)  # 和按 Ctrl-C 一樣：離開 consumer group 再結束
    for program in programs:
        program.wait(timeout=15)


def place_order() -> str:
    with TestClient(create_postgres_app()) as client:  # 和 POST /orders 走同一段程式，不用另外開 API
        order = client.post("/orders", json=A001).json()
    print(f"\n下單 → {order['order_id']} {order['status']}，合計 {order['total']} 元\n", flush=True)
    return order["order_id"]


def wait_for(query: str, order_id: str, expected: tuple, seconds: float = 60) -> bool:
    deadline = time.monotonic() + seconds
    with psycopg.connect(config.database_url(), autocommit=True) as conn:
        while time.monotonic() < deadline:
            if conn.execute(query, (order_id,)).fetchone() == expected:
                return True
            time.sleep(0.3)
    return False


def happy() -> None:
    print("== 編舞式：付款、廚房、外送各自聽事件接力 ==", flush=True)
    programs = start()
    try:
        order_id = place_order()
        done = wait_for("SELECT status FROM deliveries WHERE order_id = %s", order_id, ("assigned",))
    finally:
        stop(programs)
    print(f"\n{'外送員指派好了' if done else '60 秒內沒走完'}。\n")
    trace(order_id)


def decline() -> None:
    print("== 付款失敗的那一條：付款 consumer 加 FOODGO_FAULT_PAYMENT=decline ==", flush=True)
    programs = start(fault_payment="decline")
    try:
        order_id = place_order()
        done = wait_for("SELECT status FROM tickets WHERE order_id = %s", order_id, ("cancelled",))
    finally:
        stop(programs)
    print(f"\n{'廚房不做了' if done else '60 秒內沒走完'}。\n")
    trace(order_id)


# ---------------------------------------------------------------- 走到哪了？
def one(conn: psycopg.Connection, query: str, order_id: str) -> tuple | None:
    return conn.execute(query, (order_id,)).fetchone()


def messages_about(order_id: str, topics: dict[str, str]) -> list[tuple[int, str, int, int, str]]:
    """從頭讀每一個 topic（讀到現在最新的 offset 為止），只留 key＝這張訂單的：(時間 ms, topic, partition, offset, type)。"""
    consumer = Consumer(
        {"bootstrap.servers": config.kafka_bootstrap(), "group.id": "trace", "enable.auto.commit": False}
    )
    found = []
    try:
        partitions = [
            TopicPartition(topic, p, OFFSET_BEGINNING)
            for topic in topics.values()
            for p in consumer.list_topics(topic, timeout=10).topics[topic].partitions
        ]
        left = sum(high - low for low, high in (consumer.get_watermark_offsets(tp, timeout=10) for tp in partitions))
        consumer.assign(partitions)  # 四個 topic 一起從頭讀
        deadline = time.monotonic() + 10
        while left > 0 and time.monotonic() < deadline:
            message = consumer.poll(0.5)
            if message is None or message.error():
                continue
            left -= 1
            if message.key().decode() == order_id:
                kind = dict(message.headers() or []).get("type", b"?").decode()
                found.append((message.timestamp()[1], message.topic(), message.partition(), message.offset(), kind))
    finally:
        consumer.close()
    return sorted(found)


def lag_of(groups: dict[str, list[str]]) -> dict[str, int]:
    """每個 consumer group 還有幾則沒處理（lag）：最新的 offset − 已提交的 offset。"""
    lags = {}
    for group, topics in groups.items():
        consumer = Consumer(
            {"bootstrap.servers": config.kafka_bootstrap(), "group.id": group, "enable.auto.commit": False}
        )
        try:
            parts = [
                TopicPartition(t, p) for t in topics for p in consumer.list_topics(t, timeout=10).topics[t].partitions
            ]
            total = 0
            for tp in consumer.committed(parts, timeout=10):
                low, high = consumer.get_watermark_offsets(tp, timeout=10)
                total += high - (tp.offset if tp.offset >= 0 else low)
            lags[group] = total
        finally:
            consumer.close()
    return lags


def tables_about(order_id: str, url: str) -> list[tuple[str, str, str]]:
    """每個上下文自己的表裡，這張訂單是什麼樣子：(上下文, 表, 一句話)。"""
    state = EventSourcedOrderRepository(url).load(order_id).state
    with psycopg.connect(url) as conn:
        payment = one(
            conn, "SELECT payment_id, status, amount, reference, reason FROM payments WHERE order_id = %s", order_id
        )
        ticket = one(conn, "SELECT ticket_id, status FROM tickets WHERE order_id = %s", order_id)
        heard = one(conn, "SELECT outcome FROM kitchen_payments WHERE order_id = %s", order_id)
        delivery = one(
            conn, "SELECT delivery_id, status, courier_id, dropoff FROM deliveries WHERE order_id = %s", order_id
        )

    ordering = payments = kitchen = deliveries = "（沒有）"
    if state is not None:
        ordering = f"第 {state.version} 版 {state.status.value}，合計 {state.total}"
    if payment is not None:
        payment_id, status, amount, reference, reason = payment
        payments = f"{payment_id} {status} {amount} 元（{f'授權碼 {reference}' if reference else reason}）"
    if ticket is not None:
        kitchen = f"{ticket[0]} {ticket[1]}（{f'聽說付款 {heard[0]}' if heard else '還沒聽到付款的結果'}）"
    if delivery is not None:
        delivery_id, status, courier_id, dropoff = delivery
        deliveries = f"{delivery_id} {status}，{f'外送員 {courier_id}' if courier_id else '等餐廳接單'}，送到 {dropoff}"
    return [
        ("點餐", "events", ordering),
        ("付款", "payments", payments),
        ("廚房", "tickets", kitchen),
        ("外送", "deliveries", deliveries),
    ]


def trace(
    order_id: str, *, url: str | None = None, topics: dict[str, str] = TOPICS, groups: dict[str, list[str]] = GROUPS
) -> None:
    """A001 走到哪了？沒有一個地方知道：4 張表、4 個 topic、3 個 consumer group 拼起來才知道。"""
    url = url or config.database_url()
    print(f"== {order_id} 走到哪了？沒有一個地方知道全部：要查 4 張表、讀 4 個 topic ==")

    rows = tables_about(order_id, url)
    print("\n各上下文自己的表（現在同一個資料庫；第 10 集拆成 4 個）：")
    for context, table, text in rows:
        print(f"  {pad(context, 4)}  {pad(table, 10)}  {text}")

    found = messages_about(order_id, topics)
    print(
        f"\nKafka 上 key＝{order_id} 的訊息，照時間排（{len(found)} 則，分在 {len({m[1] for m in found})} 個 topic）："
    )
    print(f"  {pad('時間', 12)}  {pad('topic', 15)}  {pad('partition/offset', 16)}  type")
    for ms, topic, partition, offset, kind in found:
        clock = datetime.fromtimestamp(ms / 1000, TAIPEI).strftime("%H:%M:%S.%f")[:-3]
        print(f"  {clock}  {pad(topic, 15)}  {pad(f'{partition}/{offset}', 16)}  {kind}")

    lags = lag_of(groups)
    print("\nconsumer group 還沒處理的（lag）：" + "、".join(f"{group} {lag}" for group, lag in lags.items()))

    if " placed" in rows[0][2] and " failed " in rows[1][2]:
        print(f"\n付款失敗了，可是點餐不知道：{order_id} 還是 placed。外送還在等一張不會來的接單通知。")
        print("編舞式裡沒有人負責整條流程：誰該取消訂單、誰該告訴顧客？（第 8 集）")


PARTS = {"happy": happy, "decline": decline}


def main(args: list[str]) -> None:
    if args[:1] == ["trace"] and len(args) == 2:
        trace(args[1])
    elif len(args) <= 1 and (not args or args[0] in PARTS):
        PARTS[args[0] if args else "happy"]()
    else:
        sys.exit("用法：python demos/ep07_choreography.py [happy|decline|trace 訂單編號]")


if __name__ == "__main__":
    main(sys.argv[1:])
