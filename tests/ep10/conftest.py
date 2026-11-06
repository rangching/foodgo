"""第 10 集的 fixture：記憶體版的 span 收集器、帶 header 的假 Kafka 訊息。

整合測試的共用工具（foodgo_test 資料庫、topic、group、read_topic、沒開 Docker 自動略過）在 tests/conftest.py。
"""
import pytest

_EXPORTER = None


@pytest.fixture
def spans():
    """這個測試裡結束的 span（記憶體版，不送 Jaeger）。一個 pytest 程序只設定一次追蹤，每個測試開始前清空。

    用法：spans() 回傳到目前為止結束的 span 清單（照結束的順序）。
    """
    global _EXPORTER
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    if _EXPORTER is None:
        _EXPORTER = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))  # 結束就交出來，不用等一批
        trace.set_tracer_provider(provider)
    _EXPORTER.clear()
    return _EXPORTER.get_finished_spans


class HeaderMessage:
    """長得像 confluent_kafka.Message 的假訊息，這次有 topic 與 header（第 7 集的 FakeMessage 沒有）。"""

    def __init__(self, event, *, topic: str = "ordering.events", headers: dict[str, str] | None = None, offset: int = 0):
        self._event = event
        self._topic = topic
        self._headers = [(k, v.encode()) for k, v in {"event_id": event.event_id, "type": type(event).__name__, **(headers or {})}.items()]
        self._offset = offset

    def value(self) -> bytes:
        return self._event.to_json().encode()

    def key(self) -> bytes:
        return self._event.order_id.encode()

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return self._offset

    def headers(self) -> list[tuple[str, bytes]]:
        return self._headers


@pytest.fixture
def header_message():
    """HeaderMessage(事件, topic=, headers=)：帶 traceparent、correlation_id 的假 Kafka 訊息。"""
    return HeaderMessage
