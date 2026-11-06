"""外送的 consumer：從 Kafka 讀訊息。自己一個程序：

    uv run python -m foodgo.delivery.consumer

第 8 集起（預設，編排式）：聽 delivery.commands（AssignCourier），做完回覆到 delivery.events。
FOODGO_SAGA=choreography：第 7 集的接力，聽 ordering.events（DeliveryRequested → 開配送單）、kitchen.events（TicketAccepted → 指派）。
consumer group 都是 delivery；發的 CourierAssigned 先寫 delivery_outbox，處理完一則就送出去。
"""
import json
import threading
from collections.abc import Sequence

from confluent_kafka import Message

from foodgo import contracts
from foodgo.delivery.postgres import OUTBOX, SCHEMAS, PostgresDeliveryRepository
from foodgo.delivery.service import Dispatch
from foodgo.infra import config, db, kafka, outbox

GROUP = "delivery"
LISTENS = (contracts.DELIVERY_COMMANDS,)  # 第 8 集：只聽給自己的命令
CHOREOGRAPHY = (contracts.ORDERING_TOPIC, contracts.KITCHEN_TOPIC)  # 第 7 集：聽點餐與廚房的事件


def handle(dispatch: Dispatch, message: Message) -> None:
    """處理一則 Kafka 訊息。不是外送要的就略過。"""
    payload = message.value().decode()
    kind = json.loads(payload)["type"]
    where = f"partition {message.partition()}，offset {message.offset()}"
    print(f"外送：收到 {kind} {message.key().decode()}（{where}）", flush=True)
    if kind == "DeliveryRequested":
        dispatch.on_delivery_requested(contracts.DeliveryRequested.from_json(payload))
    elif kind == "TicketAccepted":
        dispatch.on_ticket_accepted(contracts.TicketAccepted.from_json(payload))
    elif kind == "AssignCourier":  # 第 8 集起
        dispatch.on_assign_courier(contracts.AssignCourier.from_json(payload))


def run(
    dispatch: Dispatch,
    url: str,
    *,
    listens: Sequence[str] = LISTENS,
    group: str = GROUP,
    stop_when_idle: float | None = None,
    stop: threading.Event | None = None,  # 第 10 集：服務要關了，設起來就停
) -> None:
    """收訊息；每處理完一則（和每次閒下來）就把 delivery_outbox 裡還沒送的送到 Kafka。"""
    sender = kafka.KafkaSender()

    def relay() -> None:
        outbox.relay_once(url, sender, table=OUTBOX, who="外送")

    def handle_then_relay(message: Message) -> None:
        handle(dispatch, message)
        relay()

    relay()  # 上次當掉前還沒送的，先送
    kafka.consume(listens, group, handle_then_relay, on_idle=relay, stop_when_idle=stop_when_idle, stop=stop)


def main() -> None:
    url = config.database_url()
    listens = CHOREOGRAPHY if config.saga_mode() == "choreography" else LISTENS
    db.create_tables(url, *SCHEMAS)
    for topic in (*listens, contracts.DELIVERY_TOPIC):
        kafka.ensure_topic(topic)
    dispatch = Dispatch(PostgresDeliveryRepository(url))
    print(f"外送：開始聽 {'、'.join(listens)}（consumer group：{GROUP}）", flush=True)
    try:
        run(dispatch, url, listens=listens)
    except KeyboardInterrupt:
        print("外送：停止", flush=True)


if __name__ == "__main__":
    main()
