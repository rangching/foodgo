"""Kafka 的薄薄一層（confluent-kafka）：建 topic、送訊息、以 consumer group 收訊息。"""
import socket
import threading
import time
from collections.abc import Callable, Sequence

from confluent_kafka import (
    OFFSET_BEGINNING,
    Consumer,
    KafkaError,
    KafkaException,
    Message,
    Producer,
    TopicPartition,
)
from confluent_kafka.admin import AdminClient, NewTopic

from foodgo.infra import config, telemetry


def ensure_topic(topic: str, *, partitions: int = 3, bootstrap: str | None = None, timeout: float = 10.0) -> None:
    """topic 不存在就建一個。單節點 Kafka，副本數只能是 1。

    第 11 集：建好（或別人剛建好）還不夠，要等到每個 partition 都有 leader 才回來。五個容器同時起的時候，
    別的服務剛建的 topic 已經列得出來、leader 卻還沒選好；這時 consumer 一訂閱就收到
    「Unknown topic or partition」，背景工作當掉。本機一個一個起程序時碰不到，容器一起起才撞到。
    """
    admin = AdminClient({"bootstrap.servers": bootstrap or config.kafka_bootstrap()})
    if topic not in admin.list_topics(timeout=10).topics:
        future = admin.create_topics([NewTopic(topic, num_partitions=partitions, replication_factor=1)])[topic]
        try:
            future.result()
        except KafkaException as error:
            if error.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                raise
    deadline = time.monotonic() + timeout
    while not _ready(admin, topic):
        if time.monotonic() > deadline:
            raise TimeoutError(f"topic {topic} 建好了，等了 {timeout:g} 秒還沒有 leader")
        time.sleep(0.2)


def _ready(admin: AdminClient, topic: str) -> bool:
    """這個 topic 的每個 partition 都選好 leader 了嗎（consumer 訂閱、producer 送出都要有 leader）。"""
    found = admin.list_topics(topic, timeout=5).topics.get(topic)
    if found is None or found.error is not None or not found.partitions:
        return False
    return all(p.leader >= 0 and p.error is None for p in found.partitions.values())


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
    topics: str | Sequence[str],
    group: str,
    handle: Callable[[Message], None],
    *,
    bootstrap: str | None = None,
    stop_when_idle: float | None = None,
    on_idle: Callable[[], None] | None = None,
    stop: threading.Event | None = None,
    start_at: Callable[[str, int], int | None] | None = None,
) -> None:
    """以 consumer group 的身分收訊息：處理完一則，才提交那一則的位置（offset）。

    topics：一個或好幾個 topic（第 7 集起廚房、外送都要聽兩個）。不同 topic 之間沒有先後保證。
    handle 丟例外就不提交、直接結束。重啟後從上次提交的位置繼續，沒處理完的那一則會再收到一次。
    stop_when_idle：連續幾秒沒有新訊息就結束（給測試用；平常一直收）。
    on_idle：一秒沒有新訊息就呼叫一次（第 7 集：順便把自己 outbox 裡還沒送的送出去）。
    start_at（第 9 集）：分到 partition 時問一次「這個 partition 處理到第幾則」（記在自己的資料庫裡），
      從下一則讀起；回 None 就從頭讀。這時 Kafka 記的位置只拿來看 lag，不拿來決定從哪裡讀。
    stop（第 10 集）：服務要關了——設起來之後，處理完手上這一則就離開 consumer group（服務裡的背景工作用）。
    每一則訊息一個 process span（第 10 集）：從 Kafka header 的 traceparent 接續上一段。
    """
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap or config.kafka_bootstrap(),
            "group.id": group,
            "client.id": socket.gethostname(),  # 第 12 集：K8S 裡＝Pod 的名字，describe 一眼看出哪個 partition 分給哪個 Pod
            "enable.auto.commit": False,  # 自己決定什麼時候提交
            "auto.offset.reset": "earliest",  # 新的 group 從最舊的訊息開始讀
            "session.timeout.ms": 6000,  # 成員當掉 6 秒沒心跳，partition 就交給別人
        }
    )
    assigned = False

    def on_assign(_consumer: Consumer, partitions: list[TopicPartition]) -> None:
        nonlocal assigned
        assigned = True
        if start_at is not None:
            for p in partitions:
                done = start_at(p.topic, p.partition)
                p.offset = OFFSET_BEGINNING if done is None else done + 1
            _consumer.assign(partitions)

    consumer.subscribe([topics] if isinstance(topics, str) else list(topics), on_assign=on_assign)
    idle = 0.0
    try:
        while stop is None or not stop.is_set():
            message = consumer.poll(1.0)
            if message is None:
                if on_idle is not None:
                    on_idle()
                idle += 1.0 if assigned else 0.0
                if stop_when_idle is not None and idle >= stop_when_idle:
                    return
                continue
            idle = 0.0
            if message.error():
                raise KafkaException(message.error())
            with telemetry.consuming(message, group):  # 第 10 集：接續 trace
                handle(message)  # 出錯就讓它當掉：位置沒提交，重啟後會再收到這一則
            consumer.commit(message=message, asynchronous=False)  # 處理完才提交
    finally:
        consumer.close()  # 離開 consumer group，partition 馬上交給別人


def replay(
    topics: Sequence[str],
    handle: Callable[[Message], None],
    *,
    start_at: Callable[[str, int], int | None] | None = None,
    bootstrap: str | None = None,
) -> int:
    """重播（第 9 集）：不加入任何 consumer group，每個 partition 從 start_at 的下一則（沒有就從頭）讀到「現在最新」為止。

    開始時先問好每個 partition 最新到哪（high watermark），讀到那裡就停：一定會結束，不用等閒下來。
    之後才寫進來的，留給平常的 consumer。回傳交給 handle 的訊息數。
    """
    consumer = Consumer(
        {"bootstrap.servers": bootstrap or config.kafka_bootstrap(), "group.id": "replay", "enable.auto.commit": False}
    )
    try:
        ends: dict[tuple[str, int], int] = {}
        partitions = []
        for topic in topics:
            for number in consumer.list_topics(topic, timeout=10).topics[topic].partitions:
                low, high = consumer.get_watermark_offsets(TopicPartition(topic, number), timeout=10)
                done = start_at(topic, number) if start_at is not None else None
                first = low if done is None else max(done + 1, low)
                if first < high:
                    partitions.append(TopicPartition(topic, number, first))
                    ends[(topic, number)] = high
        consumer.assign(partitions)
        count = 0
        while ends:
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error():
                raise KafkaException(message.error())
            where = (message.topic(), message.partition())
            if where not in ends:  # 這個 partition 已經讀到開始時的最新了
                continue
            handle(message)
            count += 1
            if message.offset() + 1 >= ends[where]:
                del ends[where]
        return count
    finally:
        consumer.close()


def active_members(group: str, *, bootstrap: str | None = None) -> int:
    """這個 consumer group 現在有幾個成員在線上（第 9 集：重建前先確定投影 consumer 停了）。"""
    admin = AdminClient({"bootstrap.servers": bootstrap or config.kafka_bootstrap()})
    description = admin.describe_consumer_groups([group])[group].result(timeout=10)
    return len(description.members)
