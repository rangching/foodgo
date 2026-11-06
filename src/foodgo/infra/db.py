"""PostgreSQL：建資料庫、建表。每個上下文自己寫自己的表結構，這裡只負責把它們建起來。"""
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import errors, sql


def create_tables(url: str, *schemas: str) -> None:
    """建表；已經有了就跳過。好幾個程序同時啟動也不會撞在一起（advisory lock 排隊）。"""
    with psycopg.connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(4)")
        for schema in schemas:
            conn.execute(schema)


def ensure_database(url: str) -> bool:
    """第 10 集：url 指的資料庫不存在就建一個（每個服務自己一個）。建了回 True，本來就有回 False。

    CREATE DATABASE 不能在交易裡跑，也不能連著要建的那一個：先連同一台的 postgres 資料庫，autocommit。
    """
    parts = urlsplit(url)
    name = parts.path.lstrip("/")
    admin = urlunsplit(parts._replace(path="/postgres"))
    with psycopg.connect(admin, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            return False
        try:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except (errors.DuplicateDatabase, errors.UniqueViolation):  # 同時有別人也在建
            return False
    return True
