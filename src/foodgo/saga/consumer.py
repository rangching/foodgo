"""編排器的 consumer：聽點餐的事實與每個參與者的回覆，送命令到各自的命令 topic。自己一個程序：

    uv run python -m foodgo.saga.consumer

聽：ordering.events、payment.events、kitchen.events、delivery.events（consumer group saga）。
發：ordering.commands、payment.commands、kitchen.commands、delivery.commands（先寫 saga_outbox，處理完一則就送出去）。
FOODGO_SAGA_TIMEOUT＝等回覆幾秒（預設 3）；FOODGO_SAGA_RETRIES＝逾時或退款失敗後再送幾次（預設 1）。
"""
from collections.abc import Sequence

from confluent_kafka import Message

from foodgo import contracts
from foodgo.infra import config, db, kafka, outbox
from foodgo.saga.machine import Settings
from foodgo.saga.orchestrator import Orchestrator
from foodgo.saga.postgres import OUTBOX, SCHEMAS, PostgresSagaRepository

GROUP = "saga"
LISTENS = (contracts.ORDERING_TOPIC, contracts.PAYMENT_TOPIC, contracts.KITCHEN_TOPIC, contracts.DELIVERY_TOPIC)


def handle(orchestrator: Orchestrator, message: Message) -> None:
    """處理一則 Kafka 訊息：聽得懂的交給 saga，其他略過。"""
    orchestrator.on_payload(message.value().decode())


def run(
    orchestrator: Orchestrator,
    url: str,
    *,
    listens: Sequence[str] = LISTENS,
    group: str = GROUP,
    stop_when_idle: float | None = None,
) -> None:
    """收訊息；每處理完一則、每閒下來一秒：看逾時，再把 saga_outbox 裡的命令送出去。"""
    sender = kafka.KafkaSender()

    def tick_then_relay() -> None:
        orchestrator.tick()
        outbox.relay_once(url, sender, table=OUTBOX, who="編排器")

    def handle_then_relay(message: Message) -> None:
        handle(orchestrator, message)
        tick_then_relay()

    tick_then_relay()  # 停機的時候到期的，先處理
    kafka.consume(listens, group, handle_then_relay, on_idle=tick_then_relay, stop_when_idle=stop_when_idle)


def main() -> None:
    url = config.database_url()
    db.create_tables(url, *SCHEMAS)
    for topic in (*LISTENS, *contracts.COMMAND_TOPICS.values()):
        kafka.ensure_topic(topic)
    settings = Settings.from_environment()
    orchestrator = Orchestrator(PostgresSagaRepository(url), settings)
    print(
        f"編排器：開始聽 {'、'.join(LISTENS)}（consumer group：{GROUP}；"
        f"等回覆 {settings.timeout:g} 秒、重試 {settings.retries} 次）",
        flush=True,
    )
    try:
        run(orchestrator, url)
    except KeyboardInterrupt:
        print("編排器：停止", flush=True)


if __name__ == "__main__":
    main()
