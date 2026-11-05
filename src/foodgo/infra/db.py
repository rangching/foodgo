"""PostgreSQL：建表。每個上下文自己寫自己的表結構，這裡只負責把它們建起來。"""
import psycopg


def create_tables(url: str, *schemas: str) -> None:
    """建表；已經有了就跳過。好幾個程序同時啟動也不會撞在一起（advisory lock 排隊）。"""
    with psycopg.connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(4)")
        for schema in schemas:
            conn.execute(schema)
