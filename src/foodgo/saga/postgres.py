"""saga 存在 PostgreSQL：sagas（現在走到哪）、saga_log（每一步、每個補償）、saga_outbox（要送的命令）。

一則回覆進來＝一個交易：記帳（processed_messages）＋改 sagas＋寫 saga_log＋命令寫進 saga_outbox。
編排器當掉重啟，從 sagas 接著做；逾時看 deadline 欄位，不靠記憶體裡的計時器。
"""
from collections.abc import Mapping
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from foodgo import contracts
from foodgo.infra import inbox, outbox
from foodgo.saga.machine import Entry, OrderSaga, SagaState
from foodgo.saga.repository import Apply

SCHEMA = """
CREATE TABLE IF NOT EXISTS sagas (
    saga_id      text        PRIMARY KEY,      -- 一張訂單一個 saga：就是 order_id
    type         text        NOT NULL,         -- 哪一種 saga（現在只有 OrderSaga）
    state        text        NOT NULL,         -- started／payment_pending／…／completed／cancelled／needs_attention
    data         jsonb       NOT NULL,         -- 點餐給的事實（金額、地址、菜名）與一路收到的編號
    waiting_for  text        NOT NULL,         -- 正在等哪個命令的回覆；空字串＝沒在等
    attempts     integer     NOT NULL,         -- 這個命令送了幾次
    deadline     timestamptz,                  -- 等到什麼時候；NULL＝不限時
    done         jsonb       NOT NULL,         -- 做成的正向步驟
    todo         jsonb       NOT NULL,         -- 還沒送的補償命令
    reason       text        NOT NULL,         -- 為什麼要倒回去
    version      integer     NOT NULL,         -- 存了幾次
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sagas_deadline ON sagas (deadline) WHERE deadline IS NOT NULL;

CREATE TABLE IF NOT EXISTS saga_log (
    id        bigserial   PRIMARY KEY,         -- 寫進來的順序
    saga_id   text        NOT NULL,
    at        timestamptz NOT NULL DEFAULT clock_timestamp(),  -- 同一個交易裡的每一列也分得出先後
    state     text        NOT NULL,            -- 寫這一列時的狀態
    action    text        NOT NULL,            -- receive／send／timeout／retry／compensate／finish／give_up／ignore
    message   text        NOT NULL,            -- 收到或送出的訊息，例如 AuthorizePayment
    detail    text        NOT NULL             -- 給人看的一句話
);
CREATE INDEX IF NOT EXISTS saga_log_saga ON saga_log (saga_id, id);
"""
OUTBOX = "saga_outbox"  # 編排器要送的命令先寫這裡，consumer 再送到各自的命令 topic
SCHEMAS = (SCHEMA, outbox.schema(OUTBOX), inbox.SCHEMA)

CONSUMER = "saga"  # processed_messages 裡用這個名字記帳
COLUMNS = "saga_id, state, data, waiting_for, attempts, deadline, done, todo, reason, version"


class PostgresSagaRepository:
    def __init__(self, url: str, *, topics: Mapping[str, str] = contracts.COMMAND_TOPICS) -> None:
        self._url = url
        self._topics = dict(topics)  # 接收者 → 命令 topic（測試換成臨時的 topic）

    def change(self, saga_id: str, apply: Apply, *, message_id: str | None = None) -> OrderSaga | None:
        with psycopg.connect(self._url) as conn:  # 一個交易：一起成功、一起失敗
            if message_id is not None and not inbox.first_time(conn, CONSUMER, message_id):
                return None
            row = conn.execute(f"SELECT {COLUMNS} FROM sagas WHERE saga_id = %s FOR UPDATE", (saga_id,)).fetchone()
            saga = apply(None if row is None else _saga(row))
            if saga is None:
                return None
            commands, entries = saga.pull()
            saga.version += 1
            conn.execute(
                "INSERT INTO sagas (saga_id, type, state, data, waiting_for, attempts, deadline, done, todo, reason, version)"
                " VALUES (%s, 'OrderSaga', %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (saga_id) DO UPDATE SET state = EXCLUDED.state, data = EXCLUDED.data,"
                " waiting_for = EXCLUDED.waiting_for, attempts = EXCLUDED.attempts, deadline = EXCLUDED.deadline,"
                " done = EXCLUDED.done, todo = EXCLUDED.todo, reason = EXCLUDED.reason, version = EXCLUDED.version,"
                " updated_at = now()",
                (
                    saga.saga_id,
                    saga.state.value,
                    Jsonb(saga.data),
                    saga.waiting_for,
                    saga.attempts,
                    saga.deadline,
                    Jsonb(saga.done),
                    Jsonb(saga.todo),
                    saga.reason,
                    saga.version,
                ),
            )
            conn.cursor().executemany(
                "INSERT INTO saga_log (saga_id, state, action, message, detail) VALUES (%s, %s, %s, %s, %s)",
                [(saga.saga_id, e.state.value, e.action, e.message, e.detail) for e in entries],
            )
            for command in commands:
                outbox.put(conn, topic=self._topics[command.receiver], key=saga.saga_id, message=command, table=OUTBOX)
        return saga

    def get(self, saga_id: str) -> OrderSaga | None:
        with psycopg.connect(self._url) as conn:
            row = conn.execute(f"SELECT {COLUMNS} FROM sagas WHERE saga_id = %s", (saga_id,)).fetchone()
        return None if row is None else _saga(row)

    def due(self, now: datetime) -> list[str]:
        with psycopg.connect(self._url) as conn:
            rows = conn.execute("SELECT saga_id FROM sagas WHERE deadline <= %s ORDER BY deadline", (now,)).fetchall()
        return [saga_id for (saga_id,) in rows]

    def log(self, saga_id: str) -> list[Entry]:
        with psycopg.connect(self._url) as conn:
            rows = conn.execute(
                "SELECT state, action, message, detail FROM saga_log WHERE saga_id = %s ORDER BY id", (saga_id,)
            ).fetchall()
        return [Entry(SagaState(state), action, message, detail) for state, action, message, detail in rows]


def _saga(row: tuple) -> OrderSaga:
    saga_id, state, data, waiting_for, attempts, deadline, done, todo, reason, version = row
    return OrderSaga(
        saga_id,
        SagaState(state),
        data,
        waiting_for,
        attempts,
        deadline,
        list(done),
        list(todo),
        reason,
        version,
    )
