"""第 7 集的測試工具：長得像 Kafka 訊息的假訊息、四個臨時 topic。

整合測試的共用工具（foodgo_test 資料庫、group、read_topic、沒開 Docker 自動略過）在 tests/conftest.py。
"""
import uuid
from types import SimpleNamespace

import pytest


class FakeMessage:
    """長得像 confluent_kafka.Message 的假訊息：單元測試不用開 Kafka。"""

    def __init__(self, event, offset: int = 0) -> None:
        self._value = event.to_json().encode()
        self._key = event.order_id.encode()
        self._offset = offset

    def value(self) -> bytes:
        return self._value

    def key(self) -> bytes:
        return self._key

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return self._offset


@pytest.fixture
def kafka_message():
    """FakeMessage(事件, offset)：把整合事件包成假的 Kafka 訊息。"""
    return FakeMessage


@pytest.fixture
def topics():
    """四個臨時 topic（點餐、付款、廚房、外送各一個），測完刪掉，Kafka UI 上不會留下來。"""
    from confluent_kafka.admin import AdminClient

    from foodgo.infra import config, kafka

    suffix = uuid.uuid4().hex[:8]
    names = SimpleNamespace(**{ctx: f"test.{ctx}.{suffix}" for ctx in ("ordering", "payment", "kitchen", "delivery")})
    for name in vars(names).values():
        kafka.ensure_topic(name)
    yield names
    admin = AdminClient({"bootstrap.servers": config.kafka_bootstrap()})
    for future in admin.delete_topics(list(vars(names).values())).values():
        future.result(timeout=30)


@pytest.fixture
def messages_in():
    """一個 topic 裡現在有的所有訊息（從頭讀到最新的 offset 為止，不用等逾時）。"""
    from confluent_kafka import Consumer, TopicPartition

    from foodgo.infra import config

    def read(topic: str) -> list:
        consumer = Consumer(
            {"bootstrap.servers": config.kafka_bootstrap(), "group.id": "unused", "enable.auto.commit": False}
        )
        try:
            partitions = [
                TopicPartition(topic, p, 0) for p in consumer.list_topics(topic, timeout=10).topics[topic].partitions
            ]
            left = sum(consumer.get_watermark_offsets(tp, timeout=10)[1] for tp in partitions)  # 先問好，再開始讀
            consumer.assign(partitions)
            messages = []
            while left > 0:
                message = consumer.poll(1.0)
                if message is not None and not message.error():
                    messages.append(message)
                    left -= 1
            return sorted(messages, key=lambda m: m.timestamp()[1])
        finally:
            consumer.close()

    return read
