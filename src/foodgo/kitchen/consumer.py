"""廚房的 consumer：從 Kafka 讀點餐的事件，收到 OrderPlaced 就開廚房單。和 API 分開，自己一個程序：

    uv run python -m foodgo.kitchen.consumer

FOODGO_FAULT_KITCHEN=crash：一收到 OrderPlaced 就當掉（訊息留在 Kafka，修好重啟會再收到）。
"""
import json
import os

from confluent_kafka import Message

from foodgo import contracts
from foodgo.infra import config, db, kafka
from foodgo.kitchen.postgres import SCHEMAS, PostgresTicketRepository
from foodgo.kitchen.service import Kitchen

GROUP = "kitchen"  # consumer group：同一組裡的成員分著讀 partition，位置（offset）記在這個名字下


def handle(kitchen: Kitchen, message: Message) -> None:
    """處理一則 Kafka 訊息。不是廚房要的事件就略過。"""
    payload = message.value().decode()
    kind = json.loads(payload)["type"]
    where = f"partition {message.partition()}，offset {message.offset()}"
    print(f"廚房：收到 {kind} {message.key().decode()}（{where}）", flush=True)
    if kind == "OrderPlaced":
        kitchen.on_order_placed(contracts.OrderPlaced.from_json(payload))


def main() -> None:
    url = config.database_url()
    db.create_tables(url, *SCHEMAS)
    kafka.ensure_topic(contracts.ORDERING_TOPIC)
    kitchen = Kitchen(tickets=PostgresTicketRepository(url), fault=os.environ.get("FOODGO_FAULT_KITCHEN", ""))
    print(f"廚房：開始聽 {contracts.ORDERING_TOPIC}（consumer group：{GROUP}）", flush=True)
    try:
        kafka.consume(contracts.ORDERING_TOPIC, GROUP, lambda message: handle(kitchen, message))
    except KeyboardInterrupt:
        print("廚房：停止", flush=True)


if __name__ == "__main__":
    main()
