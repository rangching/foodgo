"""廚房單存在 PostgreSQL。開單和「這則訊息處理過了」寫在同一個交易：重送幾次都只開一張。

第 7 集起：kitchen_payments 記住聽到的付款結果；要公告的事件寫進廚房自己的 outbox（kitchen_outbox）。
第 8 集起（編排式）：AcceptTicket 命令帶著菜名來，開單、決定、回覆在同一個交易；kitchen_payments 只有編舞式在用。
"""
from collections.abc import Sequence

import psycopg
from psycopg.types.json import Jsonb

from foodgo import contracts
from foodgo.infra import inbox, outbox
from foodgo.kitchen.ticket import Dish, Ticket, TicketStatus

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS ticket_number;
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id      text        PRIMARY KEY,
    order_id       text        NOT NULL,      -- 只記編號，不認識點餐的表
    restaurant_id  text        NOT NULL,
    dishes         jsonb       NOT NULL,      -- [{"name", "quantity"}]，沒有價格
    note           text        NOT NULL,
    status         text        NOT NULL,      -- received／accepted／rejected／cancelled／ready
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kitchen_payments (
    order_id   text        PRIMARY KEY,       -- 廚房聽說的付款結果（第 7 集）：付款的事件可能比 OrderPlaced 先到
    outcome    text        NOT NULL,          -- authorized／failed
    reason     text        NOT NULL,
    heard_at   timestamptz NOT NULL DEFAULT now()
);
"""
OUTBOX = "kitchen_outbox"  # 廚房要公告的事件先寫這裡，consumer 再送到 kitchen.events
SCHEMAS = (SCHEMA, inbox.SCHEMA, outbox.schema(OUTBOX))  # 廚房的資料庫要有這些表

CONSUMER = "kitchen"  # processed_messages 裡用這個名字記帳


class PostgresTicketRepository:
    def __init__(self, url: str, *, topic: str = contracts.KITCHEN_TOPIC) -> None:
        self._url = url
        self._topic = topic

    def next_id(self) -> str:
        with psycopg.connect(self._url) as conn:
            (number,) = conn.execute("SELECT nextval('ticket_number')").fetchone()
        return f"T{number:03d}"

    def get(self, ticket_id: str) -> Ticket | None:
        rows = self._select("WHERE ticket_id = %s", (ticket_id,))
        return rows[0] if rows else None

    def by_order(self, order_id: str) -> Ticket | None:
        rows = self._select("WHERE order_id = %s", (order_id,))
        return rows[0] if rows else None

    def all(self) -> list[Ticket]:
        return self._select("ORDER BY created_at, ticket_id", ())

    def seen(self, message_id: str) -> bool:
        with psycopg.connect(self._url) as conn:
            return inbox.seen(conn, CONSUMER, message_id)

    def add(
        self, ticket: Ticket, *, message_id: str | None = None, announce: Sequence[contracts.IntegrationEvent] = ()
    ) -> bool:
        with psycopg.connect(self._url) as conn:  # 一個交易：記帳＋開單（＋第 8 集的回覆），一起成功、一起失敗
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
            for message in announce:
                outbox.put(conn, topic=self._topic, key=ticket.order_id, message=message, table=OUTBOX)
        return True

    def save(self, ticket: Ticket, *, announce: Sequence[contracts.IntegrationEvent] = ()) -> None:
        with psycopg.connect(self._url) as conn:  # 一個交易：新狀態＋要公告的事件
            conn.execute("UPDATE tickets SET status = %s WHERE ticket_id = %s", (ticket.status.value, ticket.ticket_id))
            for message in announce:
                outbox.put(conn, topic=self._topic, key=ticket.order_id, message=message, table=OUTBOX)

    def note_payment(self, order_id: str, outcome: str, reason: str = "", *, message_id: str) -> bool:
        with psycopg.connect(self._url) as conn:  # 一個交易：記帳＋記下付款結果
            if not inbox.first_time(conn, CONSUMER, message_id):
                return False
            conn.execute(
                "INSERT INTO kitchen_payments (order_id, outcome, reason) VALUES (%s, %s, %s)"
                " ON CONFLICT (order_id) DO NOTHING",
                (order_id, outcome, reason),
            )
        return True

    def payment_of(self, order_id: str) -> tuple[str, str] | None:
        with psycopg.connect(self._url) as conn:
            row = conn.execute(
                "SELECT outcome, reason FROM kitchen_payments WHERE order_id = %s", (order_id,)
            ).fetchone()
        return None if row is None else (row[0], row[1])

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
