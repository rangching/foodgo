"""廚房單存在 PostgreSQL。開單和「這則訊息處理過了」寫在同一個交易：重送幾次都只開一張。"""
import psycopg
from psycopg.types.json import Jsonb

from foodgo.infra import inbox
from foodgo.kitchen.ticket import Dish, Ticket, TicketStatus

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS ticket_number;
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id      text        PRIMARY KEY,
    order_id       text        NOT NULL,      -- 只記編號，不認識點餐的表
    restaurant_id  text        NOT NULL,
    dishes         jsonb       NOT NULL,      -- [{"name", "quantity"}]，沒有價格
    note           text        NOT NULL,
    status         text        NOT NULL,      -- received／ready
    created_at     timestamptz NOT NULL DEFAULT now()
);
"""
SCHEMAS = (SCHEMA, inbox.SCHEMA)  # 廚房的資料庫要有這兩張表

CONSUMER = "kitchen"  # processed_messages 裡用這個名字記帳


class PostgresTicketRepository:
    def __init__(self, url: str) -> None:
        self._url = url

    def next_id(self) -> str:
        with psycopg.connect(self._url) as conn:
            (number,) = conn.execute("SELECT nextval('ticket_number')").fetchone()
        return f"T{number:03d}"

    def get(self, ticket_id: str) -> Ticket | None:
        rows = self._select("WHERE ticket_id = %s", (ticket_id,))
        return rows[0] if rows else None

    def all(self) -> list[Ticket]:
        return self._select("ORDER BY created_at, ticket_id", ())

    def seen(self, message_id: str) -> bool:
        with psycopg.connect(self._url) as conn:
            return inbox.seen(conn, CONSUMER, message_id)

    def add(self, ticket: Ticket, *, message_id: str | None = None) -> bool:
        with psycopg.connect(self._url) as conn:  # 一個交易：記帳＋開單，一起成功、一起失敗
            if message_id is not None and not inbox.first_time(conn, CONSUMER, message_id):
                return False  # 別人剛處理過這一則
            conn.execute(
                "INSERT INTO tickets (ticket_id, order_id, restaurant_id, dishes, note, status)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    ticket.ticket_id,
                    ticket.order_id,
                    ticket.restaurant_id,
                    Jsonb([{"name": d.name, "quantity": d.quantity} for d in ticket.dishes]),
                    ticket.note,
                    ticket.status.value,
                ),
            )
        return True

    def save(self, ticket: Ticket) -> None:
        with psycopg.connect(self._url) as conn:
            conn.execute("UPDATE tickets SET status = %s WHERE ticket_id = %s", (ticket.status.value, ticket.ticket_id))

    def _select(self, where: str, params: tuple) -> list[Ticket]:
        with psycopg.connect(self._url) as conn:
            rows = conn.execute(
                f"SELECT ticket_id, order_id, restaurant_id, dishes, note, status FROM tickets {where}", params
            ).fetchall()
        return [
            Ticket(
                ticket_id,
                order_id,
                restaurant_id,
                tuple(Dish(d["name"], d["quantity"]) for d in dishes),
                note,
                TicketStatus(status),
            )
            for ticket_id, order_id, restaurant_id, dishes, note, status in rows
        ]
