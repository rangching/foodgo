"""Kafka 的薄薄一層（confluent-kafka）：建 topic、送訊息、以 consumer group 收訊息。"""
from collections.abc import Callable

from confluent_kafka import Consumer, KafkaError, KafkaException, Message, Producer
from confluent_kafka.admin import AdminClient, NewTopic

from foodgo.infra import config


def ensure_topic(topic: str, *, partitions: int = 3, bootstrap: str | None = None) -> None:
    """topic 不存在就建一個。單節點 Kafka，副本數只能是 1。"""
    admin = AdminClient({"bootstrap.servers": bootstrap or config.kafka_bootstrap()})
    if topic in admin.list_topics(timeout=10).topics:
        return
    future = admin.create_topics([NewTopic(topic, num_partitions=partitions, replication_factor=1)])[topic]
    try:
        future.result()
    except KafkaException as error:
        if error.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
            raise


class KafkaSender:
    """送訊息到 Kafka。send() 先放進緩衝區，flush() 等 Kafka 確認全部收到。"""

    def __init__(self, bootstrap: str | None = None) -> None:
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap or config.kafka_bootstrap(),
                "acks": "all",  # 寫進 Kafka 才算數
                "enable.idempotence": True,  # 網路重試時，Kafka 不會因為重試多存一份
                "message.timeout.ms": 10000,  # 10 秒送不到就算失敗
            }
        )
        self._errors: list[str] = []

    def send(self, topic: str, key: str, value: str, headers: dict[str, str]) -> None:
        self._producer.produce(
            topic,
            key=key.encode(),
            value=value.encode(),
            headers=[(name, text.encode()) for name, text in headers.items()],
            on_delivery=self._delivered,
        )

    def _delivered(self, error: KafkaError | None, message: Message) -> None:
        if error is not None:
            self._errors.append(str(error))

    def flush(self) -> None:
        left = self._producer.flush(30)
        errors, self._errors = self._errors, []
        if left or errors:
            raise KafkaException(f"Kafka 沒有確認收到：還有 {left} 則在路上，失敗 {errors}")


def consume(
    topic: str,
    group: str,
    handle: Callable[[Message], None],
    *,
    bootstrap: str | None = None,
    stop_when_idle: float | None = None,
) -> None:
    """以 consumer group 的身分收訊息：處理完一則，才提交那一則的位置（offset）。

    handle 丟例外就不提交、直接結束。重啟後從上次提交的位置繼續，沒處理完的那一則會再收到一次。
    stop_when_idle：連續幾秒沒有新訊息就結束（給測試用；平常一直收）。
    """
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap or config.kafka_bootstrap(),
            "group.id": group,
            "enable.auto.commit": False,  # 自己決定什麼時候提交
            "auto.offset.reset": "earliest",  # 新的 group 從最舊的訊息開始讀
            "session.timeout.ms": 6000,  # 成員當掉 6 秒沒心跳，partition 就交給別人
        }
    )
    assigned = False

    def on_assign(_consumer: Consumer, _partitions: list) -> None:
        nonlocal assigned
        assigned = True

    consumer.subscribe([topic], on_assign=on_assign)
    idle = 0.0
    try:
        while True:
            message = consumer.poll(1.0)
            if message is None:
                idle += 1.0 if assigned else 0.0
                if stop_when_idle is not None and idle >= stop_when_idle:
                    return
                continue
            idle = 0.0
            if message.error():
                raise KafkaException(message.error())
            handle(message)  # 出錯就讓它當掉：位置沒提交，重啟後會再收到這一則
            consumer.commit(message=message, asynchronous=False)  # 處理完才提交
    finally:
        consumer.close()  # 離開 consumer group，partition 馬上交給別人
