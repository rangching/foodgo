"""訂單存在 PostgreSQL。存訂單時，要對外公告的事件寫進 outbox——同一個交易，一起成功、一起失敗。"""
import psycopg
from psycopg.types.json import Jsonb

from foodgo import contracts
from foodgo.infra import outbox
from foodgo.ordering.integration import to_integration
from foodgo.ordering.model import Address, Money, Order, OrderLine, OrderStatus

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS order_number;
CREATE TABLE IF NOT EXISTS orders (
    order_id          text        PRIMARY KEY,
    customer_id       text        NOT NULL,
    restaurant_id     text        NOT NULL,
    delivery_address  text        NOT NULL,
    delivery_fee      integer     NOT NULL,   -- 元
    note              text        NOT NULL,
    status            text        NOT NULL,   -- draft／placed／cancelled
    lines             jsonb       NOT NULL,   -- [{"name", "unit_price", "quantity"}]
    updated_at        timestamptz NOT NULL DEFAULT now()
);
"""
SCHEMAS = (SCHEMA, outbox.SCHEMA)  # 點餐的資料庫要有這兩張表

UPSERT = """
INSERT INTO orders (order_id, customer_id, restaurant_id, delivery_address, delivery_fee, note, status, lines)
VALUES (%(order_id)s, %(customer_id)s, %(restaurant_id)s, %(delivery_address)s, %(delivery_fee)s,
        %(note)s, %(status)s, %(lines)s)
ON CONFLICT (order_id) DO UPDATE SET status = EXCLUDED.status, lines = EXCLUDED.lines, updated_at = now()
"""


class PostgresOrderRepository:
    def __init__(self, url: str, *, topic: str = contracts.ORDERING_TOPIC) -> None:
        self._url = url
        self._topic = topic

    def next_id(self) -> str:
        with psycopg.connect(self._url) as conn:
            (number,) = conn.execute("SELECT nextval('order_number')").fetchone()
        return f"A{number:03d}"

    def get(self, order_id: str) -> Order | None:
        with psycopg.connect(self._url) as conn:
            row = conn.execute(
                "SELECT customer_id, restaurant_id, delivery_address, delivery_fee, note, status, lines"
                " FROM orders WHERE order_id = %s",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        customer_id, restaurant_id, street, fee, note, status, lines = row
        return Order.restore(
            order_id=order_id,
            customer_id=customer_id,
            restaurant_id=restaurant_id,
            delivery_address=Address(street),
            delivery_fee=Money(fee),
            note=note,
            lines=[OrderLine(line["name"], Money(line["unit_price"]), line["quantity"]) for line in lines],
            status=OrderStatus(status),
        )

    def save(self, order: Order) -> None:
        messages = [m for m in map(to_integration, order.pull_events()) if m is not None]  # OrderCancelled 不公告
        with psycopg.connect(self._url) as conn:  # 一個交易：離開 with 才 commit，中間出錯就全部 rollback
            conn.execute(UPSERT, row_of(order))
            for message in messages:
                outbox.put(conn, topic=self._topic, key=order.order_id, message=message)
        for message in messages:
            print(f"點餐：{order.order_id} 存進資料庫，{type(message).__name__} 寫進 outbox（同一個交易）", flush=True)


def row_of(order: Order) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "customer_id": order.customer_id,
        "restaurant_id": order.restaurant_id,
        "delivery_address": order.delivery_address.street,
        "delivery_fee": order.delivery_fee.amount,
        "note": order.note,
        "status": order.status.value,
        "lines": Jsonb(
            [{"name": line.name, "unit_price": line.unit_price.amount, "quantity": line.quantity} for line in order.lines]
        ),
    }
