"""事件儲存（PostgreSQL）：events 表只能往後加，snapshots 表放快照。

這裡只認 type＋data（jsonb）：不認識任何事件類別，也不知道快照裡裝什麼。
「OrderPlaced 這個 type 要變回哪個類別」由各上下文自己決定（點餐的在 foodgo.ordering.codec）。
和 outbox.put 一樣，每個函式都用呼叫的人給的連線：寫事件和寫 outbox 才能在同一個交易。
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    position     bigserial   PRIMARY KEY,      -- 全域順序：所有事件流排在同一條線上（第 9 集投影照這個讀）
    stream_id    text        NOT NULL,         -- 哪一條事件流：一張訂單一條，例如 A001
    version      integer     NOT NULL,         -- 這條流的第幾個事件：1、2、3…
    event_id     uuid        NOT NULL UNIQUE,  -- 事件自己的編號：同一個事件不會寫兩次
    type         text        NOT NULL,         -- 事件名稱，例如 OrderPlaced
    data         jsonb       NOT NULL,         -- 事件的內容
    occurred_at  timestamptz NOT NULL,         -- 事件發生的時間：時光機照這個查
    UNIQUE (stream_id, version)                -- 同一條流不會有兩個第 5 版：兩個人同時寫，只有一個寫得進去
);

-- 只能往後加：改、刪、清空都由資料庫自己擋下來
CREATE OR REPLACE FUNCTION events_are_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '事件只能往後加，不能改、不能刪（%）', TG_OP;
END $$;
CREATE OR REPLACE TRIGGER events_are_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON events
    FOR EACH STATEMENT EXECUTE FUNCTION events_are_append_only();

CREATE TABLE IF NOT EXISTS snapshots (
    stream_id   text        NOT NULL,
    version     integer     NOT NULL,          -- 套完第幾個事件之後的樣子
    state       jsonb       NOT NULL,          -- 那一刻的狀態
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stream_id, version)
);
"""


class VersionConflict(Exception):
    """寫入時，這條流已經不是呼叫的人以為的版本：有人先寫了。"""

    def __init__(self, stream_id: str, expected: int, actual: int) -> None:
        super().__init__(f"{stream_id}：預期第 {expected} 版，現在是第 {actual} 版")
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class NewEvent:
    """要寫進去的一個事件。版本號和 position 由這裡編，不用給。"""

    event_id: UUID
    type: str
    data: dict[str, Any]
    occurred_at: datetime


@dataclass(frozen=True)
class StoredEvent:
    """events 表的一列。"""

    position: int
    stream_id: str
    version: int
    event_id: UUID
    type: str
    data: dict[str, Any]
    occurred_at: datetime


def version(conn: psycopg.Connection, stream_id: str) -> int:
    """這條流現在是第幾版（有幾個事件）；還不存在就是 0。"""
    (current,) = conn.execute(
        "SELECT coalesce(max(version), 0) FROM events WHERE stream_id = %s", (stream_id,)
    ).fetchone()
    return current


def append(conn: psycopg.Connection, stream_id: str, events: Sequence[NewEvent], *, expected_version: int) -> int:
    """接在第 expected_version 個事件後面，回傳寫完的版本。版本對不上就丟 VersionConflict，一個都不寫。

    兩道關卡：先看現在的版本（擋下讀到舊資料的人）；兩個連線同時通過第一關時，
    UNIQUE (stream_id, version) 只讓先 COMMIT 的那個寫進去，另一個會等到它結束、再被擋下。
    """
    actual = version(conn, stream_id)
    if actual != expected_version:
        raise VersionConflict(stream_id, expected_version, actual)
    try:
        with conn.transaction():  # 呼叫的人的交易裡開一個 savepoint：被擋下時只退回這一段，還能查現在的版本
            conn.cursor().executemany(
                "INSERT INTO events (stream_id, version, event_id, type, data, occurred_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (stream_id, number, event.event_id, event.type, Jsonb(event.data), event.occurred_at)
                    for number, event in enumerate(events, start=expected_version + 1)
                ],
            )
    except psycopg.errors.UniqueViolation:
        raise VersionConflict(stream_id, expected_version, version(conn, stream_id)) from None
    return expected_version + len(events)


def read(
    conn: psycopg.Connection, stream_id: str, *, after_version: int = 0, until: datetime | None = None
) -> list[StoredEvent]:
    """照版本順序讀一條流。after_version：從第幾版之後開始（接在快照後面）；until：只讀那個時間點以前發生的。"""
    query = (
        "SELECT position, stream_id, version, event_id, type, data, occurred_at FROM events"
        " WHERE stream_id = %s AND version > %s"
    )
    params: list[Any] = [stream_id, after_version]
    if until is not None:
        query += " AND occurred_at <= %s"
        params.append(until)
    rows = conn.execute(query + " ORDER BY version", params).fetchall()
    return [StoredEvent(*row) for row in rows]


def save_snapshot(conn: psycopg.Connection, stream_id: str, version: int, state: dict[str, Any]) -> None:
    """存一份快照：套完第 version 個事件之後的狀態。同一版已經存過就算了。"""
    conn.execute(
        "INSERT INTO snapshots (stream_id, version, state) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (stream_id, version, Jsonb(state)),
    )


def latest_snapshot(conn: psycopg.Connection, stream_id: str) -> tuple[int, dict[str, Any]] | None:
    """最新的一份快照：(版本, 狀態)；沒有就是 None。"""
    row = conn.execute(
        "SELECT version, state FROM snapshots WHERE stream_id = %s ORDER BY version DESC LIMIT 1", (stream_id,)
    ).fetchone()
    return None if row is None else (row[0], row[1])
