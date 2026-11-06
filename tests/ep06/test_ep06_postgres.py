"""第 6 集：點餐改成事件溯源，存在 PostgreSQL——events 表、併發衝突、快照、時光機。

執行（這個檔全部要 Docker）：
  docker compose up -d && uv run pytest tests/ep06 -q
沒開 Docker 時，這個檔的測試會 skipped。
"""
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from foodgo.infra import kafka, outbox
from foodgo.ordering.event_sourcing import EventSourcedOrderRepository, PostgresEventStore
from foodgo.ordering.fold import fold
from foodgo.ordering.model import Address, Money, Order, OrderStatus
from foodgo.shared.event_store import ConcurrencyConflict

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
TAIPEI = timezone(timedelta(hours=8))


def at(clock: str) -> datetime:
    """'15:00:00' → 2026-11-06 15:00:00（臺北時間），和 conftest.py 的 a001_afternoon 同一天。"""
    hour, minute, second = map(int, clock.split(":"))
    return datetime(2026, 11, 6, hour, minute, second, tzinfo=TAIPEI)

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
    order.place()
    return order


# ---------------------------------------------------------------- events 表：只能往後加
def test_the_events_table_has_these_columns(sql):
    columns = sql("SELECT column_name FROM information_schema.columns WHERE table_name = 'events' ORDER BY ordinal_position")
    assert [c for (c,) in columns] == ["position", "stream_id", "version", "event_id", "type", "data", "occurred_at"]


def test_append_then_load_gives_the_same_events_back(db_url, a001_afternoon):
    store = PostgresEventStore(db_url)
    assert store.version("A001") == 0
    assert store.append("A001", a001_afternoon, expected_version=0) == 5
    assert store.version("A001") == 5
    assert store.load("A001") == a001_afternoon  # 同樣的類別、同樣的 event_id、同樣的時間
    assert [(r.version, r.type) for r in store.records("A001")] == [
        (1, "OrderDrafted"), (2, "ItemAdded"), (3, "ItemAdded"), (4, "OrderPlaced"), (5, "OrderCancelled")
    ]
    assert store.load("A001", after_version=3) == a001_afternoon[3:]


def test_a_row_is_a_type_plus_jsonb(db_url, sql, a001_afternoon):
    PostgresEventStore(db_url).append("A001", a001_afternoon, expected_version=0)
    [(kind, data, event_id)] = sql("SELECT type, data, event_id FROM events WHERE stream_id = 'A001' AND version = 2")
    assert kind == "ItemAdded"
    assert data == {"order_id": "A001", "name": "牛肉麵", "unit_price": 160, "quantity": 1}
    assert event_id == a001_afternoon[1].event_id
    assert sql("SELECT data->>'reason' FROM events WHERE type = 'OrderCancelled'") == [("顧客改變心意",)]


def test_position_lines_up_every_stream(db_url, sql, a001_afternoon):
    store = PostgresEventStore(db_url)
    store.append("A001", a001_afternoon[:4], expected_version=0)
    store.append("A002", a001("A002").pull_events(), expected_version=0)
    store.append("A001", a001_afternoon[4:], expected_version=4)
    rows = sql("SELECT position, stream_id, version FROM events ORDER BY position")
    assert [(s, v) for _, s, v in rows] == [("A001", n) for n in range(1, 5)] + [("A002", n) for n in range(1, 5)] + [("A001", 5)]
    positions = [p for p, _, _ in rows]
    assert positions == sorted(positions) and len(set(positions)) == 9  # 所有事件流排在同一條線上（第 9 集投影用）


def test_events_cannot_be_changed_or_removed(db_url, a001_afternoon):
    PostgresEventStore(db_url).append("A001", a001_afternoon, expected_version=0)
    for statement in ["UPDATE events SET type = 'Oops'", "DELETE FROM events", "TRUNCATE events"]:
        with psycopg.connect(db_url) as conn, pytest.raises(psycopg.errors.RaiseException, match="只能往後加"):
            conn.execute(statement)
    assert PostgresEventStore(db_url).version("A001") == 5


# ---------------------------------------------------------------- 樂觀併發：版本對不上就不寫
def test_the_wrong_expected_version_writes_nothing(db_url, a001_afternoon):
    store = PostgresEventStore(db_url)
    store.append("A001", a001_afternoon[:4], expected_version=0)
    for wrong in [3, 5, 0]:
        with pytest.raises(ConcurrencyConflict) as conflict:
            store.append("A001", a001_afternoon[4:], expected_version=wrong)
        assert (conflict.value.expected, conflict.value.actual) == (wrong, 4)
    assert store.version("A001") == 4


def test_the_database_itself_refuses_a_second_version_5(db_url, sql, a001_afternoon):
    PostgresEventStore(db_url).append("A001", a001_afternoon, expected_version=0)
    with pytest.raises(psycopg.errors.UniqueViolation, match="events_stream_id_version_key"):
        sql("INSERT INTO events (stream_id, version, event_id, type, data, occurred_at)"
            " VALUES ('A001', 5, gen_random_uuid(), 'OrderCancelled', '{}', now())")


def waiting_for_a_lock(url: str) -> int:
    with psycopg.connect(url) as conn:
        (count,) = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
        ).fetchone()
    return count


def test_two_connections_at_once_only_one_wins(db_url, sql, a001_afternoon):
    store = PostgresEventStore(db_url)
    store.append("A001", a001_afternoon[:4], expected_version=0)
    customer = Order.from_state(fold(store.load("A001")))  # 兩邊都讀到第 4 版
    support = Order.from_state(fold(store.load("A001")))
    customer.cancel("顧客改變心意")
    support.cancel("餐廳太忙")

    first = psycopg.connect(db_url)
    store.append("A001", customer.pull_events(), expected_version=4, conn=first)  # 寫了，交易還沒結束

    outcome: list[object] = []

    def second() -> None:
        with psycopg.connect(db_url) as conn:
            try:
                outcome.append(store.append("A001", support.pull_events(), expected_version=4, conn=conn))
            except ConcurrencyConflict as conflict:
                outcome.append(conflict)

    thread = threading.Thread(target=second)
    thread.start()
    deadline = time.monotonic() + 10
    while waiting_for_a_lock(db_url) == 0:  # 第二個連線卡在 UNIQUE 索引上，等第一個交易結束
        assert time.monotonic() < deadline, "第二個連線應該要在等鎖"
        time.sleep(0.05)
    assert outcome == []
    first.commit()
    first.close()
    thread.join(timeout=10)

    [conflict] = outcome
    assert isinstance(conflict, ConcurrencyConflict) and (conflict.expected, conflict.actual) == (4, 5)
    assert sql("SELECT version, data->>'reason' FROM events WHERE type = 'OrderCancelled'") == [(5, "顧客改變心意")]


# ---------------------------------------------------------------- Repository：存＝append＋outbox，讀＝fold
def test_save_appends_the_events_and_the_outbox_row_together(db_url, sql, topic):
    order = a001()
    EventSourcedOrderRepository(db_url, topic=topic).save(order)
    assert sql("SELECT version, type FROM events ORDER BY version") == [
        (1, "OrderDrafted"), (2, "ItemAdded"), (3, "ItemAdded"), (4, "OrderPlaced")
    ]
    [(event_id, kind, payload)] = sql("SELECT event_id, type, payload::text FROM outbox WHERE type = 'OrderPlaced'")
    assert kind == "OrderPlaced" and json.loads(payload)["data"]["order_id"] == "A001"  # 第 7 集起還有給付款、外送的兩則
    assert sql("SELECT event_id::text FROM events WHERE type = 'OrderPlaced'") == [(event_id,)]  # 同一件事、同一個編號
    assert sql("SELECT count(*) FROM orders") == [(0,)]  # 不再寫第 4 集的 orders 表


def test_if_the_outbox_write_fails_no_event_is_written(db_url, sql, monkeypatch):
    def broken_put(*args, **kwargs):
        raise RuntimeError("寫 outbox 時當機")

    monkeypatch.setattr(outbox, "put", broken_put)
    with pytest.raises(RuntimeError):
        EventSourcedOrderRepository(db_url).save(a001())
    assert sql("SELECT count(*) FROM events") == [(0,)]  # 同一個交易：一起失敗


def test_get_rebuilds_the_order_from_its_events(db_url):
    repo = EventSourcedOrderRepository(db_url)
    assert [repo.next_id(), repo.next_id()] == ["A001", "A002"]
    order = a001()
    repo.save(order)

    again = EventSourcedOrderRepository(db_url).get("A001")  # 另一個 Repository：真的從事件算回來
    assert again is not order and again == order
    assert (again.status, again.total, again.note, again.version) == (OrderStatus.PLACED, Money(230), "不要香菜", 4)
    assert [(line.name, line.unit_price) for line in again.lines] == [("牛肉麵", Money(160)), ("紅茶", Money(40))]
    assert repo.get("A999") is None


def test_saving_an_old_copy_is_a_conflict(db_url):
    repo = EventSourcedOrderRepository(db_url)
    repo.save(a001())
    customer, support = repo.get("A001"), repo.get("A001")
    customer.cancel("顧客改變心意")
    support.cancel("餐廳太忙")
    repo.save(customer)
    with pytest.raises(ConcurrencyConflict, match="有人先寫了"):
        repo.save(support)
    assert repo.get("A001").version == 5


class SomeoneElseWritesFirst(EventSourcedOrderRepository):
    """讀完、還沒寫之前，客服後台搶先取消了這張訂單——像另一個請求剛好插進來。"""

    armed = False

    def get(self, order_id: str) -> Order | None:
        order = super().get(order_id)
        if self.armed:
            self.armed = False
            other = EventSourcedOrderRepository(self._url)
            theirs = other.get(order_id)
            theirs.cancel("餐廳太忙")
            other.save(theirs)
        return order


def test_the_api_answers_409_when_another_request_wrote_first(db_url):
    from foodgo.app import create_app

    orders = SomeoneElseWritesFirst(db_url)
    client = TestClient(create_app(orders=orders))
    assert client.post("/orders", json=A001).status_code == 201
    orders.armed = True
    response = client.post("/orders/A001/cancel", json={"reason": "顧客改變心意"})
    assert response.status_code == 409
    assert response.json()["detail"] == "A001：你讀到的是第 4 版，現在已經是第 5 版（有人先寫了）"
    again = client.get("/orders/A001").json()
    assert (again["status"], again["version"]) == ("cancelled", 5)  # 寫進去的是客服後台那一筆


# ---------------------------------------------------------------- 快照：不用每次從第 1 個事件算起
def test_a_snapshot_means_fewer_events_to_read(db_url, sql):
    repo = EventSourcedOrderRepository(db_url)
    repo.save(a001())
    before = repo.load("A001")
    assert (before.snapshot_version, before.events_read, before.state.version) == (0, 4, 4)

    assert repo.take_snapshot("A001") == 4
    assert sql("SELECT stream_id, version, state->>'status' FROM snapshots") == [("A001", 4, "placed")]
    order = repo.get("A001")
    order.cancel("顧客改變心意")
    repo.save(order)

    after = repo.load("A001")
    assert (after.snapshot_version, after.events_read, after.state.version) == (4, 1, 5)
    assert after.state == fold(repo.events.load("A001"))  # 和從頭算的一樣
    assert after.state.status is OrderStatus.CANCELLED


# ---------------------------------------------------------------- 時光機：那個時間點長怎樣
def test_the_time_machine_answers_what_it_looked_like_then(db_url, a001_afternoon):
    PostgresEventStore(db_url).append("A001", a001_afternoon, expected_version=0)
    repo = EventSourcedOrderRepository(db_url)
    assert repo.load_as_of("A001", at("14:49:00")) is None  # 還沒有這張訂單
    then = repo.load_as_of("A001", at("14:50:30"))
    assert (then.version, then.status, then.total) == (2, OrderStatus.DRAFT, Money(190))
    three_pm = repo.load_as_of("A001", at("15:00:00"))
    assert (three_pm.version, three_pm.status, three_pm.total) == (4, OrderStatus.PLACED, Money(230))
    assert repo.load_as_of("A001", at("15:20:00")).status is OrderStatus.CANCELLED  # 剛好那一刻：算進去
    with pytest.raises(ValueError, match="時區"):
        repo.load_as_of("A001", at("15:00:00").replace(tzinfo=None))


def test_the_time_machine_through_the_api(db_url, a001_afternoon):
    from foodgo.app import create_app

    PostgresEventStore(db_url).append("A001", a001_afternoon, expected_version=0)
    client = TestClient(create_app(orders=EventSourcedOrderRepository(db_url)))
    three_pm = client.get("/orders/A001", params={"as_of": "2026-11-06T15:00:00"}).json()  # 沒寫時區：當臺北時間
    assert (three_pm["status"], three_pm["version"], three_pm["total"]) == ("placed", 4, 230)
    assert client.get("/orders/A001", params={"as_of": "2026-11-06T07:00:00Z"}).json()["status"] == "placed"  # UTC 7 點＝臺北 3 點
    assert client.get("/orders/A001", params={"as_of": "2026-11-06T14:00:00"}).status_code == 404
    assert client.get("/orders/A001").json()["status"] == "cancelled"

    history = client.get("/orders/A001/events").json()
    assert [(e["version"], e["type"]) for e in history][-2:] == [(4, "OrderPlaced"), (5, "OrderCancelled")]
    assert history[-1]["occurred_at"] == "2026-11-06T15:20:00+08:00"
    assert history[-1]["data"] == {"order_id": "A001", "reason": "顧客改變心意"}


# ---------------------------------------------------------------- 組裝點：點餐改用事件溯源，outbox 照樣送
def test_the_postgres_app_is_event_sourced(db_url, sql):
    from foodgo.app import create_postgres_app

    with TestClient(create_postgres_app(db_url)) as client:
        assert client.post("/orders", json=A001).json()["version"] == 4
    with TestClient(create_postgres_app(db_url)) as restarted:
        assert restarted.post("/orders/A001/cancel", json={"reason": "顧客改變心意"}).json()["version"] == 5
    assert sql("SELECT count(*) FROM events WHERE stream_id = 'A001'") == [(5,)]
    kinds = [kind for (kind,) in sql("SELECT type FROM outbox ORDER BY id")]
    assert kinds[0] == "OrderPlaced" and "OrderCancelled" not in kinds  # OrderCancelled 還是不對外公告（第 7 集起送出時多兩則）


def test_the_kitchen_still_gets_the_order_through_kafka(db_url, topic, group):
    from foodgo.app import create_app
    from foodgo.kitchen.consumer import handle
    from foodgo.kitchen.postgres import PostgresTicketRepository
    from foodgo.kitchen.service import Kitchen

    client = TestClient(
        create_app(
            orders=EventSourcedOrderRepository(db_url, topic=topic),
            kitchen=Kitchen(tickets=PostgresTicketRepository(db_url)),
        )
    )
    assert client.post("/orders", json=A001).status_code == 201
    outbox.relay_once(db_url, kafka.KafkaSender())
    kitchen = Kitchen(tickets=PostgresTicketRepository(db_url))
    kafka.consume(topic, group(), lambda m: handle(kitchen, m), stop_when_idle=3)
    [ticket] = client.get("/tickets").json()
    assert (ticket["order_id"], ticket["note"]) == ("A001", "不要香菜")


# ---------------------------------------------------------------- 示範腳本
def test_the_demo_runs_against_postgres(test_db_url):
    env = {**os.environ, "FOODGO_DATABASE_URL": test_db_url, "FOODGO_DEMO_DB": "foodgo_test_demo"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "demos" / "ep06_event_store.py")],
        capture_output=True, text=True, encoding="utf-8", env=env, check=True,
    )
    out = result.stdout
    for words in ["OrderDrafted", "從 5 個事件重建", "ConcurrencyConflict", "等鎖", "讀 1 個事件", "下午 3 點", "230 元", "還沒有這張訂單"]:
        assert words in out, words
