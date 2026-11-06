"""投影 consumer：聽每個上下文公告的事件，投影成 order_tracking（訂單追蹤）。自己一個程序：

    uv run python -m foodgo.tracking.consumer

聽：ordering.events、payment.events、kitchen.events、delivery.events，和編排器的 saga.events（consumer group tracking）。
不發任何事件、不送任何命令：讀取端只負責把事實排成畫面要的樣子。
從哪裡讀起看 projection_positions（和 order_tracking 同一個資料庫、同一個交易），不看 Kafka 記的位置。
"""
import json
from collections.abc import Sequence

from confluent_kafka import Message

from foodgo import contracts
from foodgo.infra import config, db, kafka
from foodgo.tracking.postgres import SCHEMAS, PostgresTrackingRepository
from foodgo.tracking.projection import STATUS, decode

GROUP = "tracking"
LISTENS = (
    contracts.ORDERING_TOPIC,
    contracts.PAYMENT_TOPIC,
    contracts.KITCHEN_TOPIC,
    contracts.DELIVERY_TOPIC,
    contracts.SAGA_TOPIC,
)


def handle(tracking: PostgresTrackingRepository, message: Message, *, quiet: bool = False) -> None:
    """一則 Kafka 訊息：投影要的就投影，其他略過。同一個 offset 第二次來，什麼都不做。"""
    payload = message.value().decode()
    kind = json.loads(payload)["type"]
    where = f"{message.topic()} {message.partition()}／offset {message.offset()}"
    row = tracking.project(message.topic(), message.partition(), message.offset(), decode(payload))
    if quiet:
        return
    if row is None:
        print(f"投影：{kind} 略過（{where}：處理過了，或不是追蹤要的）", flush=True)
    else:
        print(f"投影：{row.order_id} {kind} → {row.status}（{STATUS[row.status]}；{where}）", flush=True)


def run(
    tracking: PostgresTrackingRepository,
    *,
    listens: Sequence[str] = LISTENS,
    group: str = GROUP,
    stop_when_idle: float | None = None,
) -> None:
    """收訊息、投影。分到 partition 時，從 projection_positions 記的下一則讀起（沒有記就從頭）。"""
    kafka.consume(
        listens,
        group,
        lambda message: handle(tracking, message),
        start_at=tracking.start_at,
        stop_when_idle=stop_when_idle,
    )


def main() -> None:
    url = config.database_url()
    db.create_tables(url, *SCHEMAS)
    for topic in LISTENS:
        kafka.ensure_topic(topic)
    print(f"投影：開始聽 {'、'.join(LISTENS)}（consumer group：{GROUP}）→ order_tracking", flush=True)
    try:
        run(PostgresTrackingRepository(url))
    except KeyboardInterrupt:
        print("投影：停止", flush=True)


if __name__ == "__main__":
    main()
