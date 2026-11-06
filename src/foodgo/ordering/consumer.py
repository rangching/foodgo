"""點餐的 consumer（第 8 集）：聽編排器給點餐的命令。自己一個程序：

    uv run python -m foodgo.ordering.consumer

聽：ordering.commands（CancelOrder）；consumer group ordering。
做：Order.cancel(理由) → events 表多一個 OrderCancelled、outbox 多一則 OrderCancelled（同一個交易），送到 ordering.events。
點餐也只是編排器的一個參與者：和付款、廚房、外送一樣，收命令、做自己的事、公告結果。
"""
import json
from collections.abc import Sequence

from confluent_kafka import Message

from foodgo import contracts
from foodgo.infra import config, db, kafka, outbox
from foodgo.ordering.event_sourcing import SCHEMAS, EventSourcedOrderRepository
from foodgo.ordering.model import OrderRuleViolation, OrderStatus
from foodgo.ordering.repository import OrderRepository
from foodgo.shared.event_store import ConcurrencyConflict

GROUP = "ordering"
LISTENS = (contracts.ORDERING_COMMANDS,)


def on_cancel_order(orders: OrderRepository, command: contracts.CancelOrder, *, attempts: int = 3) -> None:
    """取消訂單。已經取消了就什麼都不做（重送也安全）。讀完到寫之間有人先改了（API 也在寫），重讀再試。"""
    for _ in range(attempts):
        order = orders.get(command.order_id)
        if order is None:
            print(f"點餐：沒有訂單 {command.order_id}，不用取消", flush=True)
            return
        if order.status is OrderStatus.CANCELLED:
            print(f"點餐：{order.order_id} 已經取消了，不再取消", flush=True)
            return
        try:
            order.cancel(command.reason)
        except OrderRuleViolation as violation:
            print(f"點餐：{order.order_id} 不能取消（{violation}）", flush=True)
            return
        try:
            orders.save(order)
        except ConcurrencyConflict as conflict:
            print(f"點餐：{conflict}，重新讀一次", flush=True)
            continue
        print(f"點餐：{order.order_id} 已取消（{command.reason}），公告 OrderCancelled", flush=True)
        return
    raise ConcurrencyConflict(command.order_id, -1, -1)


def handle(orders: OrderRepository, message: Message) -> None:
    """處理一則 Kafka 訊息。不是點餐要的命令就略過。"""
    payload = message.value().decode()
    kind = json.loads(payload)["type"]
    where = f"partition {message.partition()}，offset {message.offset()}"
    print(f"點餐：收到 {kind} {message.key().decode()}（{where}）", flush=True)
    if kind == "CancelOrder":
        on_cancel_order(orders, contracts.CancelOrder.from_json(payload))


def run(
    orders: OrderRepository,
    url: str,
    *,
    listens: Sequence[str] = LISTENS,
    group: str = GROUP,
    stop_when_idle: float | None = None,
) -> None:
    """收命令；每處理完一則（和每次閒下來）就把 outbox 裡還沒送的送到 Kafka（和 relay 程序一起送也不會重複）。"""
    sender = kafka.KafkaSender()

    def relay() -> None:
        outbox.relay_once(url, sender, who="點餐")

    def handle_then_relay(message: Message) -> None:
        handle(orders, message)
        relay()

    kafka.consume(listens, group, handle_then_relay, on_idle=relay, stop_when_idle=stop_when_idle)


def main() -> None:
    url = config.database_url()
    db.create_tables(url, *SCHEMAS)
    for topic in (*LISTENS, contracts.ORDERING_TOPIC):
        kafka.ensure_topic(topic)
    print(f"點餐：開始聽 {'、'.join(LISTENS)}（consumer group：{GROUP}）", flush=True)
    try:
        run(EventSourcedOrderRepository(url), url)
    except KeyboardInterrupt:
        print("點餐：停止", flush=True)


if __name__ == "__main__":
    main()
