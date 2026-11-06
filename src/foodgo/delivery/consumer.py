"""外送的 consumer：從 Kafka 讀點餐與廚房的事件。自己一個程序：

    uv run python -m foodgo.delivery.consumer

聽：ordering.events（DeliveryRequested → 開配送單）、kitchen.events（TicketAccepted → 指派外送員）；consumer group delivery。
發：delivery.events（CourierAssigned，先寫 delivery_outbox，處理完一則就送出去）。
"""
import json
from collections.abc import Sequence

from confluent_kafka import Message

from foodgo import contracts
from foodgo.delivery.postgres import OUTBOX, SCHEMAS, PostgresDeliveryRepository
from foodgo.delivery.service import Dispatch
from foodgo.infra import config, db, kafka, outbox

GROUP = "delivery"
LISTENS = (contracts.ORDERING_TOPIC, contracts.KITCHEN_TOPIC)


def handle(dispatch: Dispatch, message: Message) -> None:
    """處理一則 Kafka 訊息。不是外送要的事件就略過。"""
    payload = message.value().decode()
    kind = json.loads(payload)["type"]
    where = f"partition {message.partition()}，offset {message.offset()}"
    print(f"外送：收到 {kind} {message.key().decode()}（{where}）", flush=True)
    if kind == "DeliveryRequested":
        dispatch.on_delivery_requested(contracts.DeliveryRequested.from_json(payload))
    elif kind == "TicketAccepted":
        dispatch.on_ticket_accepted(contracts.TicketAccepted.from_json(payload))


def run(
    dispatch: Dispatch,
    url: str,
    *,
    listens: Sequence[str] = LISTENS,
    group: str = GROUP,
    stop_when_idle: float | None = None,
) -> None:
    """收訊息；每處理完一則（和每次閒下來）就把 delivery_outbox 裡還沒送的送到 Kafka。"""
    sender = kafka.KafkaSender()

    def relay() -> None:
        outbox.relay_once(url, sender, table=OUTBOX, who="外送")

    def handle_then_relay(message: Message) -> None:
        handle(dispatch, message)
        relay()

    relay()  # 上次當掉前還沒送的，先送
    kafka.consume(listens, group, handle_then_relay, on_idle=relay, stop_when_idle=stop_when_idle)


def main() -> None:
    url = config.database_url()
    db.create_tables(url, *SCHEMAS)
    for topic in (*LISTENS, contracts.DELIVERY_TOPIC):
        kafka.ensure_topic(topic)
    dispatch = Dispatch(PostgresDeliveryRepository(url))
    print(f"外送：開始聽 {'、'.join(LISTENS)}（consumer group：{GROUP}）", flush=True)
    try:
        run(dispatch, url)
    except KeyboardInterrupt:
        print("外送：停止", flush=True)


if __name__ == "__main__":
    main()
