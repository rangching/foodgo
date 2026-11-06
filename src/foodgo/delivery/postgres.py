"""配送單存在 PostgreSQL：deliveries 表、delivery_tickets（聽說餐廳接單了）、外送自己的 outbox。"""
from collections.abc import Sequence

import psycopg

from foodgo import contracts
from foodgo.delivery.delivery import Delivery, DeliveryStatus
from foodgo.infra import inbox, outbox

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS delivery_number;
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id  text        PRIMARY KEY,
    order_id     text        NOT NULL UNIQUE,   -- 只記編號
    pickup       text        NOT NULL,          -- 取餐點（餐廳編號）
    dropoff      text        NOT NULL,          -- 送達點（地址）
    status       text        NOT NULL,          -- waiting／assigned
    courier_id   text        NOT NULL,          -- 還沒指派是空字串
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS delivery_tickets (
    order_id   text        PRIMARY KEY,         -- 外送聽說「餐廳接單了」：TicketAccepted 可能比 DeliveryRequested 先到
    ticket_id  text        NOT NULL,
    heard_at   timestamptz NOT NULL DEFAULT now()
);
"""
OUTBOX = "delivery_outbox"  # 外送要公告的事件先寫這裡，consumer 再送到 delivery.events
SCHEMAS = (SCHEMA, outbox.schema(OUTBOX), inbox.SCHEMA)

CONSUMER = "delivery"  # processed_messages 裡用這個名字記帳


class PostgresDeliveryRepository:
    def __init__(self, url: str, *, topic: str = contracts.DELIVERY_TOPIC) -> None:
        self._url = url
        self._topic = topic

    def next_id(self) -> str:
        with psycopg.connect(self._url) as conn:
            (number,) = conn.execute("SELECT nextval('delivery_number')").fetchone()
        return f"D{number:03d}"

    def by_order(self, order_id: str) -> Delivery | None:
        rows = self._select("WHERE order_id = %s", (order_id,))
        return rows[0] if rows else None

    def all(self) -> list[Delivery]:
        return self._select("ORDER BY created_at, delivery_id", ())

    def seen(self, message_id: str) -> bool:
        with psycopg.connect(self._url) as conn:
            return inbox.seen(conn, CONSUMER, message_id)

    def add(self, delivery: Delivery, *, message_id: str) -> bool:
        with psycopg.connect(self._url) as conn:  # 一個交易：記帳＋開配送單
            if not inbox.first_time(conn, CONSUMER, message_id):
                return False
            conn.execute(
                "INSERT INTO deliveries (delivery_id, order_id, pickup, dropoff, status, courier_id)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    delivery.delivery_id,
                    delivery.order_id,
                    delivery.pickup,
                    delivery.dropoff,
                    delivery.status.value,
                    delivery.courier_id,
                ),
            )
        return True

    def save(self, delivery: Delivery, *, announce: Sequence[contracts.IntegrationEvent] = ()) -> None:
        with psycopg.connect(self._url) as conn:  # 一個交易：新狀態＋要公告的事件
            conn.execute(
                "UPDATE deliveries SET status = %s, courier_id = %s WHERE delivery_id = %s",
                (delivery.status.value, delivery.courier_id, delivery.delivery_id),
            )
            for message in announce:
                outbox.put(conn, topic=self._topic, key=delivery.order_id, message=message, table=OUTBOX)

    def note_ticket(self, order_id: str, ticket_id: str, *, message_id: str) -> bool:
        with psycopg.connect(self._url) as conn:
            if not inbox.first_time(conn, CONSUMER, message_id):
                return False
            conn.execute(
                "INSERT INTO delivery_tickets (order_id, ticket_id) VALUES (%s, %s) ON CONFLICT (order_id) DO NOTHING",
                (order_id, ticket_id),
            )
        return True

    def accepted_ticket(self, order_id: str) -> str | None:
        with psycopg.connect(self._url) as conn:
            row = conn.execute("SELECT ticket_id FROM delivery_tickets WHERE order_id = %s", (order_id,)).fetchone()
        return None if row is None else row[0]

    def _select(self, where: str, params: tuple) -> list[Delivery]:
        with psycopg.connect(self._url) as conn:
            rows = conn.execute(
                f"SELECT delivery_id, order_id, pickup, dropoff, status, courier_id FROM deliveries {where}", params
            ).fetchall()
        return [
            Delivery(delivery_id, order_id, pickup, dropoff, DeliveryStatus(status), courier_id)
            for delivery_id, order_id, pickup, dropoff, status, courier_id in rows
        ]
