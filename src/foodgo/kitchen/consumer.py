"""廚房的 consumer：從 Kafka 讀訊息。和 API 分開，自己一個程序：

    uv run python -m foodgo.kitchen.consumer

第 8 集起（預設，編排式）：聽 kitchen.commands（AcceptTicket、CancelTicket），做完回覆到 kitchen.events。
FOODGO_SAGA=choreography：第 7 集的接力，聽 ordering.events（OrderPlaced → 開廚房單）、payment.events（付款過了才接單）。
consumer group 都是 kitchen；發的事件（TicketAccepted／TicketRejected／TicketCancelled／MealReady）先寫 kitchen_outbox。
FOODGO_FAULT_KITCHEN=crash：一收到 OrderPlaced／AcceptTicket 就當掉（訊息留在 Kafka，修好重啟會再收到）。
FOODGO_FAULT_KITCHEN=reject：付款過了也拒單（TicketRejected；第 8 集用它示範補償）。
FOODGO_FAULT_KITCHEN=slow：每一則 AcceptTicket 都想 FOODGO_KITCHEN_SLOW_SECONDS 秒（預設 4）才回（第 8 集：逾時與重試）。
"""
import json
import os
from collections.abc import Sequence

from confluent_kafka import Message

from foodgo import contracts
from foodgo.infra import config, db, kafka, outbox
from foodgo.kitchen.postgres import OUTBOX, SCHEMAS, PostgresTicketRepository
from foodgo.kitchen.service import Kitchen

GROUP = "kitchen"  # consumer group：同一組裡的成員分著讀 partition，位置（offset）記在這個名字下
LISTENS = (contracts.KITCHEN_COMMANDS,)  # 第 8 集：只聽給自己的命令
CHOREOGRAPHY = (contracts.ORDERING_TOPIC, contracts.PAYMENT_TOPIC)  # 第 7 集：聽點餐與付款的事件


def handle(kitchen: Kitchen, message: Message) -> None:
    """處理一則 Kafka 訊息。不是廚房要的就略過。"""
    payload = message.value().decode()
    kind = json.loads(payload)["type"]
    where = f"partition {message.partition()}，offset {message.offset()}"
    print(f"廚房：收到 {kind} {message.key().decode()}（{where}）", flush=True)
    if kind == "OrderPlaced":
        kitchen.on_order_placed(contracts.OrderPlaced.from_json(payload))
    elif kind == "PaymentAuthorized":
        kitchen.on_payment_authorized(contracts.PaymentAuthorized.from_json(payload))
    elif kind == "PaymentFailed":
        kitchen.on_payment_failed(contracts.PaymentFailed.from_json(payload))
    elif kind == "AcceptTicket":  # 第 8 集起
        kitchen.on_accept_ticket(contracts.AcceptTicket.from_json(payload))
    elif kind == "CancelTicket":
        kitchen.on_cancel_ticket(contracts.CancelTicket.from_json(payload))


def run(
    kitchen: Kitchen,
    url: str,
    *,
    listens: Sequence[str] = LISTENS,
    group: str = GROUP,
    stop_when_idle: float | None = None,
) -> None:
    """收訊息；每處理完一則（和每次閒下來）就把 kitchen_outbox 裡還沒送的送到 Kafka。"""
    sender = kafka.KafkaSender()

    def relay() -> None:
        outbox.relay_once(url, sender, table=OUTBOX, who="廚房")

    def handle_then_relay(message: Message) -> None:
        handle(kitchen, message)
        relay()

    relay()  # 上次當掉前還沒送的，先送
    kafka.consume(listens, group, handle_then_relay, on_idle=relay, stop_when_idle=stop_when_idle)


def main() -> None:
    url = config.database_url()
    listens = CHOREOGRAPHY if config.saga_mode() == "choreography" else LISTENS
    db.create_tables(url, *SCHEMAS)
    for topic in (*listens, contracts.KITCHEN_TOPIC):
        kafka.ensure_topic(topic)
    kitchen = Kitchen(
        tickets=PostgresTicketRepository(url),
        fault=os.environ.get("FOODGO_FAULT_KITCHEN", ""),
        slow_seconds=float(os.environ.get("FOODGO_KITCHEN_SLOW_SECONDS", "4")),
    )
    print(f"廚房：開始聽 {'、'.join(listens)}（consumer group：{GROUP}）", flush=True)
    try:
        run(kitchen, url, listens=listens)
    except KeyboardInterrupt:
        print("廚房：停止", flush=True)


if __name__ == "__main__":
    main()
