"""付款的 consumer：從 Kafka 讀訊息，向金流服務要授權或退款。自己一個程序：

    uv run python -m foodgo.payment.consumer

第 8 集起（預設，編排式）：聽 payment.commands（AuthorizePayment、RefundPayment），做完回覆到 payment.events。
FOODGO_SAGA=choreography：第 7 集的接力，聽 ordering.events 的 PaymentRequested。
consumer group 都是 payment；發的事件先寫 payment_outbox，處理完一則就送出去。
FOODGO_FAULT_PAYMENT=decline：假的金流服務每一筆都拒絕，示範付款失敗。
FOODGO_FAULT_REFUND=fail：每一筆退款都失敗，示範補償本身失敗（第 8 集）。
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
LISTENS = (contracts.PAYMENT_COMMANDS,)  # 第 8 集：只聽給自己的命令
CHOREOGRAPHY = (contracts.ORDERING_TOPIC,)  # 第 7 集：聽點餐的事件


def handle(payments: Payments, message: Message) -> None:
    """處理一則 Kafka 訊息。不是付款要的就略過。"""
    payload = message.value().decode()
    kind = json.loads(payload)["type"]
    where = f"partition {message.partition()}，offset {message.offset()}"
    print(f"付款：收到 {kind} {message.key().decode()}（{where}）", flush=True)
    if kind == "PaymentRequested":  # 第 7 集
        payments.on_payment_requested(contracts.PaymentRequested.from_json(payload))
    elif kind == "AuthorizePayment":  # 第 8 集起
        payments.on_authorize_payment(contracts.AuthorizePayment.from_json(payload))
    elif kind == "RefundPayment":
        payments.on_refund_payment(contracts.RefundPayment.from_json(payload))


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
    listens = CHOREOGRAPHY if config.saga_mode() == "choreography" else LISTENS
    db.create_tables(url, *SCHEMAS)
    for topic in (*listens, contracts.PAYMENT_TOPIC):
        kafka.ensure_topic(topic)
    payments = Payments(gateway.from_environment(), PostgresPaymentRepository(url))
    print(f"付款：開始聽 {'、'.join(listens)}（consumer group：{GROUP}）", flush=True)
    try:
        run(payments, url, listens=listens)
    except KeyboardInterrupt:
        print("付款：停止", flush=True)


if __name__ == "__main__":
    main()
