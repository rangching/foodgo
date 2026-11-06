"""點餐改成事件溯源：訂單不再存成 orders 表的一列，而是 events 表裡的一串事件；要用的時候 fold 回來。

  PostgresEventStore           第 5 集 InMemoryEventStore 的 PostgreSQL 版：version／append／load／records 一樣
  EventSourcedOrderRepository  存＝append 新事件＋整合事件寫進 outbox（同一個交易）；讀＝快照＋之後的事件 fold
"""
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import psycopg

from foodgo import contracts
from foodgo.infra import event_store, outbox
from foodgo.ordering import codec
from foodgo.ordering.fold import OrderState, fold
from foodgo.ordering.integration import to_integration
from foodgo.ordering.model import Order
from foodgo.shared.event_store import ConcurrencyConflict, RecordedEvent
from foodgo.shared.events import DomainEvent

ORDER_NUMBER = "CREATE SEQUENCE IF NOT EXISTS order_number;"
SCHEMAS = (ORDER_NUMBER, event_store.SCHEMA, outbox.SCHEMA)  # 點餐的資料庫要有這些表


@contextmanager
def connection(url: str, conn: psycopg.Connection | None) -> Iterator[psycopg.Connection]:
    """有給連線就用它（跟著呼叫的人的交易）；沒給就自己開一個，離開時 commit。"""
    if conn is not None:
        yield conn
        return
    with psycopg.connect(url) as own:
        yield own


# ---------------------------------------------------------------- 事件儲存：介面和第 5 集一樣
class PostgresEventStore:
    """events 表。事件 ↔ type＋data 由 codec 翻譯；infra.event_store 只管 SQL。"""

    def __init__(self, url: str) -> None:
        self._url = url

    def version(self, stream_id: str, *, conn: psycopg.Connection | None = None) -> int:
        with connection(self._url, conn) as c:
            return event_store.version(c, stream_id)

    def append(
        self,
        stream_id: str,
        events: Sequence[DomainEvent],
        *,
        expected_version: int,
        conn: psycopg.Connection | None = None,
    ) -> int:
        """接在第 expected_version 個事件後面，回傳寫完的版本。對不上丟 ConcurrencyConflict（第 5 集那一個）。"""
        rows = [event_store.NewEvent(e.event_id, *codec.to_data(e), e.occurred_at) for e in events]
        with connection(self._url, conn) as c:
            try:
                return event_store.append(c, stream_id, rows, expected_version=expected_version)
            except event_store.VersionConflict as conflict:  # 基礎設施的例外，翻成點餐認得的那一個
                raise ConcurrencyConflict(conflict.stream_id, conflict.expected, conflict.actual) from None

    def load(
        self,
        stream_id: str,
        *,
        after_version: int = 0,
        until: datetime | None = None,
        conn: psycopg.Connection | None = None,
    ) -> list[DomainEvent]:
        """照版本順序讀出事件（交給 fold）。after_version：接在快照後面；until：時光機。"""
        with connection(self._url, conn) as c:
            rows = event_store.read(c, stream_id, after_version=after_version, until=until)
        return [codec.from_data(r.type, r.data, event_id=r.event_id, occurred_at=r.occurred_at) for r in rows]

    def records(self, stream_id: str, *, conn: psycopg.Connection | None = None) -> list[RecordedEvent]:
        """連同版本號、事件名稱一起看。"""
        with connection(self._url, conn) as c:
            rows = event_store.read(c, stream_id)
        return [
            RecordedEvent(
                r.stream_id,
                r.version,
                r.type,
                r.occurred_at,
                codec.from_data(r.type, r.data, event_id=r.event_id, occurred_at=r.occurred_at),
            )
            for r in rows
        ]


# ---------------------------------------------------------------- Repository
@dataclass(frozen=True)
class Loaded:
    """載入一次的結果：算出來的訂單，和它是怎麼算出來的。"""

    state: OrderState | None
    snapshot_version: int  # 從第幾版的快照接著算；0＝沒有快照，從第 1 個事件算起
    events_read: int  # 從 events 表讀了幾個事件來 fold

    def __str__(self) -> str:
        if self.snapshot_version:
            return f"從快照第 {self.snapshot_version} 版＋{self.events_read} 個事件"
        return f"從 {self.events_read} 個事件"


class EventSourcedOrderRepository:
    """訂單＝它的事件。介面和第 2 集的 OrderRepository 一樣（next_id／get／save），多了快照與時光機。"""

    def __init__(self, url: str, *, topic: str = contracts.ORDERING_TOPIC) -> None:
        self._url = url
        self._topic = topic
        self.events = PostgresEventStore(url)

    def next_id(self) -> str:
        with psycopg.connect(self._url) as conn:
            (number,) = conn.execute("SELECT nextval('order_number')").fetchone()
        return f"A{number:03d}"

    def load(self, order_id: str, *, conn: psycopg.Connection | None = None) -> Loaded:
        """有快照就從最新的快照接著 fold，沒有就從第 1 個事件算起。"""
        with connection(self._url, conn) as c:
            snapshot = event_store.latest_snapshot(c, order_id)
            start, base = (None, 0) if snapshot is None else (codec.state_from_data(snapshot[1]), snapshot[0])
            events = self.events.load(order_id, after_version=base, conn=c)
        return Loaded(fold(events, start), base, len(events))

    def get(self, order_id: str) -> Order | None:
        loaded = self.load(order_id)
        if loaded.state is None:
            return None
        print(f"點餐：{order_id} {loaded}重建，第 {loaded.state.version} 版", flush=True)
        return Order.from_state(loaded.state)  # 第 4 集的 Order.restore() 退場：狀態一律是 fold 算出來的

    def save(self, order: Order) -> None:
        """新事件接在讀到的版本後面；要公告的寫進 outbox。同一個交易：一起成功、一起失敗。"""
        events = order.pull_events()
        if not events:
            return
        expected = order.version - len(events)  # 讀進來時是第幾版
        messages = [m for m in map(to_integration, events) if m is not None]
        with psycopg.connect(self._url) as conn:
            version = self.events.append(order.order_id, events, expected_version=expected, conn=conn)
            for message in messages:
                outbox.put(conn, topic=self._topic, key=order.order_id, message=message)
        names = "、".join(type(e).__name__ for e in events)
        print(f"點餐：{order.order_id} 第 {expected + 1}～{version} 版寫進 events（{names}）", flush=True)
        for message in messages:
            print(f"點餐：{type(message).__name__} 寫進 outbox（同一個交易）", flush=True)

    # ------------------------------------------------------------ 快照
    def take_snapshot(self, order_id: str, *, conn: psycopg.Connection | None = None) -> int:
        """把現在的狀態存成快照，回傳是第幾版。之後載入就從這一版接著算。"""
        with connection(self._url, conn) as c:
            state = self.load(order_id, conn=c).state
            if state is None:
                raise KeyError(f"沒有訂單 {order_id}")
            event_store.save_snapshot(c, order_id, state.version, codec.state_to_data(state))
        return state.version

    # ------------------------------------------------------------ 時光機
    def load_as_of(self, order_id: str, when: datetime) -> OrderState | None:
        """那個時間點時這張訂單長怎樣：只 fold 那時候以前發生的事件。那時還沒有這張訂單就是 None。"""
        if when.tzinfo is None:
            raise ValueError("時間要帶時區，例如 datetime(…, tzinfo=臺北)：不然不知道是哪裡的下午 3 點")
        return fold(self.events.load(order_id, until=when))

    def history(self, order_id: str) -> list[RecordedEvent]:
        """這張訂單的每一個事件（給 GET /orders/{id}/events）。"""
        return self.events.records(order_id)
