"""付款的 consumer：從 Kafka 讀點餐的事件，收到 PaymentRequested 就向金流服務要授權。自己一個程序：

    uv run python -m foodgo.payment.consumer

聽：ordering.events（consumer group payment）；發：payment.events（先寫 payment_outbox，處理完一則就送出去）。
FOODGO_FAULT_PAYMENT=decline：假的金流服務每一筆都拒絕，示範付款失敗。
"""
import json
from collections.abc import Sequence

from confluent_kafka import Message

from foodgo import contracts
from foodgo.infra import config, db, kafka, outbox
from foodgo.payment import gateway
from foodgo.payment.postgres import OUTBOX, SCHEMAS, PostgresPaymentRepository
from foodgo.payment.service import Payments

GROUP = "payment"
LISTENS = (contracts.ORDERING_TOPIC,)


def handle(payments: Payments, message: Message) -> None:
    """處理一則 Kafka 訊息。不是付款要的事件就略過。"""
    payload = message.value().decode()
    kind = json.loads(payload)["type"]
    where = f"partition {message.partition()}，offset {message.offset()}"
    print(f"付款：收到 {kind} {message.key().decode()}（{where}）", flush=True)
    if kind == "PaymentRequested":
        payments.on_payment_requested(contracts.PaymentRequested.from_json(payload))


def run(
    payments: Payments,
    url: str,
    *,
    listens: Sequence[str] = LISTENS,
    group: str = GROUP,
    stop_when_idle: float | None = None,
) -> None:
    """收訊息；每處理完一則（和每次閒下來）就把 payment_outbox 裡還沒送的送到 Kafka。"""
    sender = kafka.KafkaSender()

    def relay() -> None:
        outbox.relay_once(url, sender, table=OUTBOX, who="付款")

    def handle_then_relay(message: Message) -> None:
        handle(payments, message)
        relay()

    relay()  # 上次當掉前還沒送的，先送
    kafka.consume(listens, group, handle_then_relay, on_idle=relay, stop_when_idle=stop_when_idle)


def main() -> None:
    url = config.database_url()
    db.create_tables(url, *SCHEMAS)
    for topic in (*LISTENS, contracts.PAYMENT_TOPIC):
        kafka.ensure_topic(topic)
    payments = Payments(gateway.from_environment(), PostgresPaymentRepository(url))
    print(f"付款：開始聽 {'、'.join(LISTENS)}（consumer group：{GROUP}）", flush=True)
    try:
        run(payments, url)
    except KeyboardInterrupt:
        print("付款：停止", flush=True)


if __name__ == "__main__":
    main()
