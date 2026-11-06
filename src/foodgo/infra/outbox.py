"""Transactional Outbox：要發的事件先寫進 outbox 表，和業務資料同一個交易；relay 再把它送到 Kafka。"""
import json
from dataclasses import dataclass, field
from typing import Protocol

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from foodgo.infra import telemetry


def schema(table: str = "outbox") -> str:
    """一張 outbox 表的結構。每個要發事件的上下文一張（第 7 集起：payment_outbox、kitchen_outbox…）。"""
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    id          bigserial   PRIMARY KEY,        -- 寫進來的順序，relay 照這個順序送
    event_id    text        NOT NULL UNIQUE,    -- 整合事件的編號：同一件事只會寫一次
    topic       text        NOT NULL,           -- 要送到哪個 Kafka topic
    key         text        NOT NULL,           -- Kafka 的 key：同一個 key 進同一個 partition
    type        text        NOT NULL,           -- 事件名稱，例如 OrderPlaced
    payload     json        NOT NULL,           -- 整合事件的 JSON 信封，原封不動
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz,                    -- 送到 Kafka 的時間；NULL＝還沒送
    headers     jsonb       NOT NULL DEFAULT '{{}}' -- 第 10 集：跟著訊息走的 header（traceparent、correlation_id）
);
CREATE INDEX IF NOT EXISTS {table}_unsent ON {table} (id) WHERE sent_at IS NULL;
-- 第 9 集以前建的表沒有 headers：補上（已經有就什麼都不做）
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS headers jsonb NOT NULL DEFAULT '{{}}';
"""


SCHEMA = schema()  # 點餐的 outbox（第 4 集就有的那一張）


class Message(Protocol):
    """foodgo.contracts 的整合事件都符合這個形狀。"""

    def to_json(self) -> str: ...


@dataclass(frozen=True)
class OutboxMessage:
    """outbox 裡的一列：一則等著送出去的事件。"""

    id: int
    event_id: str
    topic: str
    key: str
    type: str
    payload: str
    extra: dict[str, str] = field(default_factory=dict)  # 第 10 集：寫進來時抄下的 traceparent、correlation_id

    @property
    def headers(self) -> dict[str, str]:
        return {"event_id": self.event_id, "type": self.type, **self.extra}


def put(conn: psycopg.Connection, *, topic: str, key: str, message: Message, table: str = "outbox") -> None:
    """把一則要發的事件寫進 outbox。用呼叫的人給的連線，跟著它的交易一起 commit 或 rollback。

    第 10 集：順手抄下現在的 trace context 與關聯 ID（沒在追蹤就是 {}）——relay 送出時才接得回這條 trace。
    """
    payload = message.to_json()
    envelope = json.loads(payload)
    conn.execute(
        sql.SQL("INSERT INTO {} (event_id, topic, key, type, payload, headers) VALUES (%s, %s, %s, %s, %s, %s)").format(
            sql.Identifier(table)
        ),
        (envelope["event_id"], topic, key, envelope["type"], payload, Jsonb(telemetry.outgoing())),
    )


class Sender(Protocol):
    def send(self, topic: str, key: str, value: str, headers: dict[str, str]) -> None: ...

    def flush(self) -> None: ...  # 等 Kafka 確認收到；有送失敗的就丟例外


class RelayCrashed(Exception):
    """模擬 relay 當機。用環境變數 FOODGO_FAULT_RELAY=crash-after-send 打開。"""


def relay_once(
    url: str, sender: Sender, *, limit: int = 100, fault: str = "", table: str = "outbox", who: str = "relay"
) -> list[OutboxMessage]:
    """送一批還沒送的事件。Kafka 確認收到之後才標記 sent_at：中間當掉，下次會再送一次（至少一次）。

    table：送哪一張 outbox；who：印紀錄時的名字（第 7 集起，付款、廚房、外送的 consumer 自己送自己的 outbox）。
    """
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            sql.SQL(
                "SELECT id, event_id, topic, key, type, payload::text, headers FROM {}"
                " WHERE sent_at IS NULL ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED"
            ).format(sql.Identifier(table)),
            (limit,),
        ).fetchall()
        batch = [OutboxMessage(*row) for row in rows]
        for message in batch:
            with telemetry.sending(message.topic, message.key, message.type, message.headers) as headers:  # 第 10 集
                sender.send(message.topic, message.key, message.payload, headers)
        sender.flush()
        for message in batch:
            print(f"{who}：送出 {message.type} {message.key} → {message.topic}（event_id {message.event_id[:8]}…）", flush=True)

        if batch and fault == "crash-after-send":
            raise RelayCrashed("relay 當機：事件已經送到 Kafka，還沒標記 sent_at")

        conn.execute(
            sql.SQL("UPDATE {} SET sent_at = now() WHERE id = ANY(%s)").format(sql.Identifier(table)),
            ([m.id for m in batch],),
        )
    return batch
