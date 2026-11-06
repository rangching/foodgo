"""第 6 集示範：A001 存進 PostgreSQL 的 events 表——重播、兩個連線同時寫、快照、時光機。

    uv run python demos/ep06_event_store.py            # 全部跑一次
    uv run python demos/ep06_event_store.py events     # events 表：A001 的一生，只往後加
    uv run python demos/ep06_event_store.py replay     # 重播：讀事件、一個一個 fold 回來
    uv run python demos/ep06_event_store.py race       # 兩個連線同時寫 A001：一個成功、一個 ConcurrencyConflict
    uv run python demos/ep06_event_store.py snapshot   # 快照：有快照前後，載入時讀的事件數
    uv run python demos/ep06_event_store.py asof       # 時光機：下午 3 點時 A001 長怎樣

要先 docker compose up -d。用自己的資料庫 foodgo_demo（FOODGO_DEMO_DB 可改），每一段都先清空重來，
不會動到 API 用的 foodgo。事件時間固定在 2026-11-06 下午：14:50 開始點、14:51 送出、15:20 取消，每次跑出來都一樣。
"""
import json
import os
import sys
import threading
import time
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

import psycopg

from foodgo.infra import config, db
from foodgo.ordering.event_sourcing import SCHEMAS, EventSourcedOrderRepository, PostgresEventStore
from foodgo.ordering.fold import OrderState, evolve, fold
from foodgo.ordering.model import Address, Money, Order, OrderLine
from foodgo.shared.event_store import ConcurrencyConflict
from foodgo.shared.events import DomainEvent

TAIPEI = timezone(timedelta(hours=8))
DEMO_DB = os.environ.get("FOODGO_DEMO_DB", "foodgo_demo")


def at(clock: str) -> datetime:
    hour, minute, second = map(int, clock.split(":"))
    return datetime(2026, 11, 6, hour, minute, second, tzinfo=TAIPEI)


def reset() -> str:
    """清空 foodgo_demo（沒有就建一個）、建好表，回傳它的連線網址。"""
    url = config.database_url()
    with psycopg.connect(url, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DEMO_DB,)).fetchone() is None:
            conn.execute(f'CREATE DATABASE "{DEMO_DB}"')
    demo = urlunsplit(urlsplit(url)._replace(path=f"/{DEMO_DB}"))
    with psycopg.connect(demo, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    db.create_tables(demo, *SCHEMAS)
    return demo


def retime(events: list[DomainEvent], *clocks: str) -> list[DomainEvent]:
    """把事件的時間撥到固定的下午（示範要每次一樣；平常就是發生的那一刻）。"""
    return [replace(event, occurred_at=at(clock)) for event, clock in zip(events, clocks, strict=True)]


def a001_placed() -> list[DomainEvent]:
    """真的用 Order 走一遍：建立草稿、加牛肉麵、加紅茶、送出——4 個事件。"""
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
    order.place()
    return retime(order.pull_events(), "14:50:00", "14:50:20", "14:50:35", "14:51:00")


def cancelled(state: OrderState, reason: str, clock: str) -> list[DomainEvent]:
    order = Order.from_state(state)
    order.cancel(reason)
    return retime(order.pull_events(), clock)


def seed_a001(url: str) -> PostgresEventStore:
    """A001 的一生寫進 events 表：第 1～4 版一次寫，15:20 取消是第 5 版。"""
    store = PostgresEventStore(url)
    store.append("A001", a001_placed(), expected_version=0)
    store.append("A001", cancelled(fold(store.load("A001")), "顧客改變心意", "15:20:00"), expected_version=4)
    return store


# ---------------------------------------------------------------- 排版：中文字佔兩格
def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, size: int) -> str:
    return text + " " * (size - width(text))


def dishes(lines: tuple[OrderLine, ...]) -> str:
    return "、".join(f"{line.name} ×{line.quantity}" for line in lines) or "（還沒有）"


def show(state: OrderState) -> str:
    return f"{pad(state.status.value, 9)} | {pad(dishes(state.lines), 18)} | {state.total.amount:>3} 元"


def brief(state: OrderState) -> str:
    return f"第 {state.version} 版，{state.status.value}，{dishes(state.lines)}，合計 {state.total}"


def clock_of(moment: datetime) -> str:
    return moment.astimezone(TAIPEI).strftime("%H:%M:%S")


# ---------------------------------------------------------------- 1. events 表
def events() -> None:
    print("== events 表：A001 的一生寫成 5 列，只往後加 ==")
    url = reset()
    seed_a001(url)
    with psycopg.connect(url) as conn:
        rows = conn.execute("SELECT position, stream_id, version, occurred_at, type, data FROM events ORDER BY position").fetchall()
    print(f" position | stream_id | version | {pad('occurred_at', 11)} | {pad('type', 14)} | data")
    for position, stream_id, version, occurred_at, kind, data in rows:
        text = json.dumps(data, ensure_ascii=False)
        print(f" {position:>8} | {pad(stream_id, 9)} | {version:>7} | {pad(clock_of(occurred_at), 11)} | {pad(kind, 14)} | {text}")

    print("\n試著改掉一個事件：UPDATE events SET type = 'Oops' WHERE version = 5")
    try:
        with psycopg.connect(url) as conn:
            conn.execute("UPDATE events SET type = 'Oops' WHERE version = 5")
    except psycopg.errors.RaiseException as error:
        print(f"  → 資料庫擋下來：{error.diag.message_primary}")
    print(f"\npsql 也看得到：docker compose exec postgres psql -U foodgo -d {DEMO_DB} -c 'SELECT * FROM events;'")


# ---------------------------------------------------------------- 2. 重播
def replay() -> None:
    print("== 重播：從 events 表讀出 A001 的事件，一個一個 fold 回來 ==")
    url = reset()
    store = seed_a001(url)
    print(f" 版本 | {pad('套上的事件', 14)} | {pad('status', 9)} | {pad('品項', 18)} | 合計")
    state = None
    for event in store.load("A001"):  # SELECT … ORDER BY version，codec 把 type＋data 變回事件
        state = evolve(state, event)
        print(f" {state.version:>4} | {pad(type(event).__name__, 14)} | {show(state)}")
    print()
    order = EventSourcedOrderRepository(url).get("A001")  # Repository.get() 做的就是上面那個迴圈
    print(f"get(\"A001\") → Order：第 {order.version} 版，{order.status.value}，合計 {order.total}")


# ---------------------------------------------------------------- 3. 兩個連線同時寫
def waiting_for_a_lock(url: str) -> int:
    deadline = time.monotonic() + 10
    while True:
        with psycopg.connect(url) as conn:
            (count,) = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()
        if count or time.monotonic() > deadline:
            return count
        time.sleep(0.05)


def race() -> None:
    print("== 兩個連線同時寫 A001：UNIQUE (stream_id, version) 只讓一個寫進去 ==")
    url = reset()
    store = PostgresEventStore(url)
    store.append("A001", a001_placed(), expected_version=0)

    customer = Order.from_state(fold(store.load("A001")))
    support = Order.from_state(fold(store.load("A001")))
    print(f"連線 1（顧客 App）：讀到第 {customer.version} 版（{customer.status.value}）")
    print(f"連線 2（客服後台）：讀到第 {support.version} 版（{support.status.value}）")
    customer.cancel("顧客改變心意")
    support.cancel("餐廳太忙")  # 它讀到的也是 placed，所以 Order 說可以取消

    first = psycopg.connect(url)
    store.append("A001", customer.pull_events(), expected_version=4, conn=first)
    print('連線 1：cancel("顧客改變心意") → INSERT 第 5 版（交易還沒 COMMIT）')

    outcome: list[object] = []

    def second() -> None:
        with psycopg.connect(url) as conn:
            try:
                outcome.append(store.append("A001", support.pull_events(), expected_version=4, conn=conn))
            except ConcurrencyConflict as conflict:
                outcome.append(conflict)

    thread = threading.Thread(target=second)
    thread.start()
    print('連線 2：cancel("餐廳太忙") → INSERT 第 5 版……')
    print(f"  pg_stat_activity：{waiting_for_a_lock(url)} 個連線在等鎖——連線 2 卡在 UNIQUE 索引上，等連線 1 的交易結束")
    first.commit()
    first.close()
    print("連線 1：COMMIT")
    thread.join()
    [result] = outcome
    print(f"連線 2：→ {type(result).__name__}：{result}")

    stream = "、".join(f"{r.version} {r.type}" for r in store.records("A001"))
    reason = store.load("A001")[-1].reason
    print(f"\nA001 的事件流：{stream}（理由「{reason}」）")
    print("第 5 版只有一個：兩個人同時讀到第 4 版，資料庫只讓先 COMMIT 的寫進去。")


# ---------------------------------------------------------------- 4. 快照
def snapshot() -> None:
    print("== 快照：不用每次從第 1 個事件算起 ==")
    url = reset()
    store = PostgresEventStore(url)
    store.append("A001", a001_placed(), expected_version=0)
    repo = EventSourcedOrderRepository(url)

    before = repo.load("A001")
    print(f"沒有快照：load(\"A001\") 讀 {before.events_read} 個事件 → {brief(before.state)}")
    version = repo.take_snapshot("A001")
    print(f"take_snapshot(\"A001\") → snapshots 表存了 A001 第 {version} 版")
    store.append("A001", cancelled(before.state, "顧客改變心意", "15:20:00"), expected_version=4)
    print("15:20 顧客取消 → events 表多了第 5 版")

    after = repo.load("A001")
    print(
        f"有快照：  load(\"A001\") 從快照第 {after.snapshot_version} 版開始，"
        f"讀 {after.events_read} 個事件 → {brief(after.state)}"
    )
    full = fold(store.load("A001"))
    print(f"不用快照從頭算：讀 {full.version} 個事件 → 和有快照算出來的一樣：{full == after.state}")


# ---------------------------------------------------------------- 5. 時光機
def as_of() -> None:
    print("== 時光機：A001 在那個時間點長怎樣（load_as_of）==")
    url = reset()
    seed_a001(url)
    repo = EventSourcedOrderRepository(url)
    print(f" {pad('時間點', 8)} | 版本 | {pad('status', 9)} | {pad('品項', 18)} | 合計")
    for clock, label in [("14:49:00", ""), ("14:50:30", ""), ("15:00:00", "  ← 下午 3 點"), ("15:30:00", "")]:
        state = repo.load_as_of("A001", at(clock))
        if state is None:
            print(f" {clock} |    - | （還沒有這張訂單）")
        else:
            print(f" {clock} | {state.version:>4} | {show(state)}{label}")
    print("\n只 fold 那個時間點以前發生的事件：SELECT … WHERE occurred_at <= '2026-11-06 15:00:00+08'")


PARTS = {"events": events, "replay": replay, "race": race, "snapshot": snapshot, "asof": as_of}


def main(args: list[str]) -> None:
    chosen = args or list(PARTS)
    if any(name not in PARTS for name in chosen):
        sys.exit(f"用法：python demos/ep06_event_store.py [{'|'.join(PARTS)}]")
    for i, name in enumerate(chosen):
        if i:
            print()
        PARTS[name]()


if __name__ == "__main__":
    main(sys.argv[1:])
