"""處理過的訊息（processed_messages）：consumer 用 event_id 記下「這則處理過了」。冪等消費者的帳本。"""
import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    consumer      text        NOT NULL,         -- 哪一個 consumer（例如 kitchen）
    event_id      text        NOT NULL,         -- 哪一則事件
    processed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, event_id)
);
"""


def seen(conn: psycopg.Connection, consumer: str, event_id: str) -> bool:
    """這則處理過了沒？只是先看一眼；真正擋住重複的是 first_time()。"""
    row = conn.execute(
        "SELECT 1 FROM processed_messages WHERE consumer = %s AND event_id = %s", (consumer, event_id)
    ).fetchone()
    return row is not None


def first_time(conn: psycopg.Connection, consumer: str, event_id: str) -> bool:
    """記下「這則處理過了」：第一次回 True，已經有了回 False。要和業務資料寫在同一個交易。"""
    cursor = conn.execute(
        "INSERT INTO processed_messages (consumer, event_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (consumer, event_id),
    )
    return cursor.rowcount == 1
