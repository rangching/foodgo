"""訂單追蹤存在 PostgreSQL：order_tracking 一張訂單一列，畫面要的全部都在這一列；projection_positions 記讀到哪。

一則訊息＝一個交易：記下讀到哪（projection_positions）＋改 order_tracking 那一列。一起成功、一起失敗。
這兩張表是讀取端自己的：不 JOIN 任何上下文的表，砍掉了也能從 Kafka 重建。
"""
from dataclasses import astuple, fields

import psycopg
from psycopg.types.json import Jsonb

from foodgo import contracts
from foodgo.infra import positions
from foodgo.tracking.projection import Tracking, apply

NAME = "order_tracking"  # 這個投影的名字：projection_positions 裡用它記讀到哪

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_tracking (
    order_id          text        PRIMARY KEY,
    status            text        NOT NULL,   -- 整體狀態：由下面的事實推出來，存起來給 WHERE 與看板用
    restaurant_id     text        NOT NULL,   -- 以下是事實：每一欄記一件事，誰公告的寫在 projection.py
    items             jsonb       NOT NULL,   -- [{"name": "牛肉麵", "quantity": 1}, …]
    note              text        NOT NULL,
    amount            integer,                -- 合計（元）；PaymentRequested 還沒到是 NULL
    delivery_address  text        NOT NULL,
    placed_at         timestamptz,            -- OrderPlaced 還沒到是 NULL（別的 topic 的事件可能先到）
    payment           text        NOT NULL,   -- ''／authorized／failed／refund_failed／refunded
    payment_id        text        NOT NULL,
    payment_reason    text        NOT NULL,
    refunded          integer,
    kitchen           text        NOT NULL,   -- ''／accepted／rejected／ready／cancelled
    ticket_id         text        NOT NULL,
    kitchen_reason    text        NOT NULL,
    delivery          text        NOT NULL,   -- ''／assigned
    delivery_id       text        NOT NULL,
    courier_id        text        NOT NULL,
    cancelled         text        NOT NULL,   -- 取消的理由；'' ＝沒取消
    attention         text        NOT NULL,   -- 轉人工處理的理由；'' ＝不用
    updated_at        timestamptz NOT NULL    -- 最晚發生的那一則事件的時間（不是寫進來的時間：重建後才會一模一樣）
);
CREATE INDEX IF NOT EXISTS order_tracking_status ON order_tracking (status);
"""
SCHEMAS = (SCHEMA, positions.SCHEMA)

FIELDS = [f.name for f in fields(Tracking)]  # order_id, restaurant_id, items, …（和 Tracking 同一個順序）
COLUMNS = ", ".join(FIELDS)


class PostgresTrackingRepository:
    def __init__(self, url: str, *, name: str = NAME) -> None:
        self._url = url
        self._name = name

    # ------------------------------------------------------------ 投影 consumer 用
    def project(
        self, topic: str, partition: int, offset: int, event: contracts.IntegrationEvent | None
    ) -> Tracking | None:
        """一則 Kafka 訊息：記下讀到哪，是投影要的就改那一列。回傳改完的那一列；處理過了或不是投影要的回 None。"""
        with psycopg.connect(self._url) as conn:  # 一個交易
            positions.lock(conn, self._name)
            if not positions.advance(conn, self._name, topic, partition, offset):
                return None  # 這個 offset 處理過了（重啟後 Kafka 再送一次）
            if event is None:
                return None
            row = conn.execute(f"SELECT {COLUMNS} FROM order_tracking WHERE order_id = %s", (event.order_id,)).fetchone()
            tracking = apply(None if row is None else _tracking(row), event)
            values = _row(tracking)
            conn.execute(
                f"INSERT INTO order_tracking (status, {COLUMNS}) VALUES (%s, {', '.join(['%s'] * len(FIELDS))})"
                " ON CONFLICT (order_id) DO UPDATE SET status = EXCLUDED.status, "
                + ", ".join(f"{name} = EXCLUDED.{name}" for name in FIELDS[1:]),
                (tracking.status, *values),
            )
        return tracking

    def start_at(self, topic: str, partition: int) -> int | None:
        """這個 partition 處理到第幾則（給 kafka.consume／replay：從下一則讀起）。"""
        with psycopg.connect(self._url) as conn:
            return positions.last_offset(conn, self._name, topic, partition)

    # ------------------------------------------------------------ 查詢 API 用
    def get(self, order_id: str) -> Tracking | None:
        with psycopg.connect(self._url) as conn:
            row = conn.execute(f"SELECT {COLUMNS} FROM order_tracking WHERE order_id = %s", (order_id,)).fetchone()
        return None if row is None else _tracking(row)

    def as_of_position(self) -> int:
        """投影一共處理了幾則訊息（每個 partition 的 offset 從 0 起算，所以是 last_offset＋1 加起來）。"""
        with psycopg.connect(self._url) as conn:
            done = positions.read(conn, self._name)
        return sum(offset + 1 for offset in done.values())

    def all(self) -> list[Tracking]:
        with psycopg.connect(self._url) as conn:
            rows = conn.execute(f"SELECT {COLUMNS} FROM order_tracking ORDER BY order_id").fetchall()
        return [_tracking(row) for row in rows]

    # ------------------------------------------------------------ 重建用
    def clear(self) -> tuple[int, int]:
        """砍掉讀模型：清空 order_tracking、忘記讀到哪，同一個交易。回傳 (清掉幾列, 清掉幾個 partition 的位置)。"""
        with psycopg.connect(self._url) as conn:
            positions.lock(conn, self._name)
            rows = conn.execute("DELETE FROM order_tracking").rowcount
            return rows, positions.reset(conn, self._name)

    def fingerprint(self) -> tuple[int, str]:
        """整張表的指紋：(列數, 每一列照 order_id 排好接起來的 md5)。重建前後一樣＝資料一模一樣。"""
        with psycopg.connect(self._url) as conn:
            (count, digest) = conn.execute(
                "SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY order_id), '')) FROM order_tracking t"
            ).fetchone()
        return count, digest


def _row(t: Tracking) -> tuple:
    values = list(astuple(t))
    values[FIELDS.index("items")] = Jsonb([{"name": i.name, "quantity": i.quantity} for i in t.items])
    return tuple(values)


def _tracking(row: tuple) -> Tracking:
    data = dict(zip(FIELDS, row, strict=True))
    data["items"] = tuple(contracts.OrderItem(name=i["name"], quantity=i["quantity"]) for i in data["items"])
    return Tracking(**data)
