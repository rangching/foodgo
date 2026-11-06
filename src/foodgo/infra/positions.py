"""投影讀到哪（projection_positions）：每個投影、每個 partition 處理到第幾個 offset。和讀模型寫在同一個交易。

和 processed_messages（第 4 集，照 event_id 記）比：
  - 一個 partition 一列，不會越長越大；
  - 重建＝把讀模型和這幾列一起清掉，投影就從頭讀——不用動 Kafka 的 consumer group；
  - 讀模型可以說出自己「新到哪裡」（as_of_position）。
同一則事件被送了兩次、而且 offset 不同（relay 重送、參與者再回一次），靠的是投影本身冪等，不是這張表。
"""
import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS projection_positions (
    projection   text        NOT NULL,         -- 哪一個投影，例如 order_tracking
    topic        text        NOT NULL,
    partition    integer     NOT NULL,
    last_offset  bigint      NOT NULL,         -- 這個 partition 處理完的最後一則
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (projection, topic, partition)
);
"""


def lock(conn: psycopg.Connection, projection: str) -> None:
    """同一個投影的寫入排隊（交易結束自動放開）：重建和投影 consumer 不會在同一列上搶。"""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"projection:{projection}",))


def advance(conn: psycopg.Connection, projection: str, topic: str, partition: int, offset: int) -> bool:
    """記下「處理到這一則了」：比記過的新才算數，回 True；已經處理過（offset 沒有比較新）回 False。"""
    cursor = conn.execute(
        "INSERT INTO projection_positions (projection, topic, partition, last_offset) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (projection, topic, partition) DO UPDATE"
        " SET last_offset = EXCLUDED.last_offset, updated_at = now()"
        " WHERE projection_positions.last_offset < EXCLUDED.last_offset",
        (projection, topic, partition, offset),
    )
    return cursor.rowcount == 1


def last_offset(conn: psycopg.Connection, projection: str, topic: str, partition: int) -> int | None:
    """這個 partition 處理到第幾則；還沒處理過是 None（從頭讀）。"""
    row = conn.execute(
        "SELECT last_offset FROM projection_positions WHERE projection = %s AND topic = %s AND partition = %s",
        (projection, topic, partition),
    ).fetchone()
    return None if row is None else row[0]


def read(conn: psycopg.Connection, projection: str) -> dict[tuple[str, int], int]:
    """(topic, partition) → 處理到的最後一則 offset。"""
    rows = conn.execute(
        "SELECT topic, partition, last_offset FROM projection_positions WHERE projection = %s", (projection,)
    ).fetchall()
    return {(topic, partition): offset for topic, partition, offset in rows}


def reset(conn: psycopg.Connection, projection: str) -> int:
    """忘記讀到哪：下次從每個 partition 的第一則讀起。回傳清掉幾列。"""
    return conn.execute("DELETE FROM projection_positions WHERE projection = %s", (projection,)).rowcount
