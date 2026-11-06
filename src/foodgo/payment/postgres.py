"""付款存在 PostgreSQL：payments 表＋付款自己的 outbox（payment_outbox）＋處理過的訊息。"""
from collections.abc import Sequence

import psycopg

from foodgo import contracts
from foodgo.infra import inbox, outbox
from foodgo.payment.payment import Payment, PaymentStatus

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS payment_number;
CREATE TABLE IF NOT EXISTS payments (
    payment_id   text        PRIMARY KEY,
    order_id     text        NOT NULL UNIQUE,   -- 一張訂單一筆付款；只記編號，不認識點餐的表
    customer_id  text        NOT NULL,
    amount       integer     NOT NULL,          -- 元
    status       text        NOT NULL,          -- authorized／failed／refunded（第 8 集）
    reference    text        NOT NULL,          -- 金流服務的授權碼
    reason       text        NOT NULL,          -- 失敗的原因（第 8 集起也記退款失敗的原因）
    created_at   timestamptz NOT NULL DEFAULT now()
);
"""
OUTBOX = "payment_outbox"  # 付款要公告的事件先寫這裡，consumer 再送到 payment.events
SCHEMAS = (SCHEMA, outbox.schema(OUTBOX), inbox.SCHEMA)

CONSUMER = "payment"  # processed_messages 裡用這個名字記帳


class PostgresPaymentRepository:
    def __init__(self, url: str, *, topic: str = contracts.PAYMENT_TOPIC) -> None:
        self._url = url
        self._topic = topic

    def next_id(self) -> str:
        with psycopg.connect(self._url) as conn:
            (number,) = conn.execute("SELECT nextval('payment_number')").fetchone()
        return f"P{number:03d}"

    def seen(self, message_id: str) -> bool:
        with psycopg.connect(self._url) as conn:
            return inbox.seen(conn, CONSUMER, message_id)

    def by_order(self, order_id: str) -> Payment | None:
        rows = self._select("WHERE order_id = %s", (order_id,))
        return rows[0] if rows else None

    def all(self) -> list[Payment]:
        return self._select("ORDER BY created_at, payment_id", ())

    def add(self, payment: Payment, *, message_id: str, announce: Sequence[contracts.IntegrationEvent] = ()) -> bool:
        with psycopg.connect(self._url) as conn:  # 一個交易：記帳＋付款＋outbox，一起成功、一起失敗
            if not inbox.first_time(conn, CONSUMER, message_id):
                return False
            conn.execute(
                "INSERT INTO payments (payment_id, order_id, customer_id, amount, status, reference, reason)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    payment.payment_id,
                    payment.order_id,
                    payment.customer_id,
                    payment.amount,
                    payment.status.value,
                    payment.reference,
                    payment.reason,
                ),
            )
            for message in announce:
                outbox.put(conn, topic=self._topic, key=payment.order_id, message=message, table=OUTBOX)
        return True

    def save(
        self, payment: Payment | None, *, message_id: str, announce: Sequence[contracts.IntegrationEvent] = ()
    ) -> bool:
        with psycopg.connect(self._url) as conn:  # 一個交易：記帳＋付款的新樣子＋outbox
            if not inbox.first_time(conn, CONSUMER, message_id):
                return False
            if payment is not None:
                conn.execute(
                    "INSERT INTO payments (payment_id, order_id, customer_id, amount, status, reference, reason)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (payment_id) DO UPDATE"
                    " SET status = EXCLUDED.status, reference = EXCLUDED.reference, reason = EXCLUDED.reason",
                    (
                        payment.payment_id,
                        payment.order_id,
                        payment.customer_id,
                        payment.amount,
                        payment.status.value,
                        payment.reference,
                        payment.reason,
                    ),
                )
            for message in announce:
                outbox.put(conn, topic=self._topic, key=message.order_id, message=message, table=OUTBOX)
        return True

    def _select(self, where: str, params: tuple) -> list[Payment]:
        with psycopg.connect(self._url) as conn:
            rows = conn.execute(
                f"SELECT payment_id, order_id, customer_id, amount, status, reference, reason FROM payments {where}",
                params,
            ).fetchall()
        return [Payment(*row[:4], PaymentStatus(row[4]), *row[5:]) for row in rows]
