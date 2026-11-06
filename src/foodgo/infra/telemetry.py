"""分散式追蹤（第 10 集）：OpenTelemetry。一張訂單走過的每一段（span）串成一條 trace，在 Jaeger 上看。

HTTP 那一段不用自己寫：FastAPI 與 httpx 自動 instrument——送出去的請求自動帶 traceparent header，收到的自動接續。
Kafka 那一段要自己帶，分三個地方：
  1. 寫 outbox 時：把「現在的 trace context」（traceparent）和關聯 ID 抄下來，和事件存在同一列（outgoing()）；
  2. relay 送出時：從那一列接續，開一個 send span，把它的 traceparent 放進 Kafka header（sending()）；
  3. consumer 收到時：從 Kafka header 接續，開一個 process span，處理完才結束（consuming()）。
處理時又寫了 outbox（例如付款回覆 PaymentAuthorized），第 1 步抄到的就是這個 process span：一段接一段，同一條 trace。

關聯 ID（correlation ID）是另一回事：Gateway 給每個請求一個 X-Correlation-ID，跟著 HTTP header、outbox、Kafka header 一路傳，
印在紀錄裡、記在每個 span 上（foodgo.correlation_id）。trace 是給 Jaeger 畫圖用的；關聯 ID 是給人搜尋、對紀錄用的。

沒有呼叫 setup() 的程序（第 4～9 集的單一程序、單元測試）用的是 OpenTelemetry 的空實作：什麼都不記，header 也不會多。
"""
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from confluent_kafka import Message
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind

from foodgo.infra import config

CORRELATION_HEADER = "X-Correlation-ID"  # HTTP 的 header
TRACE_HEADER = "X-Trace-ID"  # 回應裡告訴客戶端：去 Jaeger 查這條 trace
CORRELATION_KEY = "correlation_id"  # outbox 與 Kafka header 裡的名字
ATTRIBUTE = "foodgo.correlation_id"  # span 上的名字：Jaeger 可以用 Tags 搜尋

_correlation: ContextVar[str] = ContextVar("correlation_id", default="")
tracer = trace.get_tracer("foodgo")


# ---------------------------------------------------------------- 設定
def setup(service: str, *, exporter=None) -> None:
    """一個程序呼叫一次：這個程序在 Jaeger 上叫什麼（service.name），span 送到哪（OTLP／HTTP → Jaeger）。

    exporter：測試時給記憶體版；不給就送到 config.otlp_endpoint()（預設 http://127.0.0.1:14318）。
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    exporter = exporter or OTLPSpanExporter(endpoint=f"{config.otlp_endpoint()}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=1000))  # 每秒送一批
    trace.set_tracer_provider(provider)


def shutdown() -> None:
    """程序結束前把還沒送出去的 span 送完。"""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()


def instrument(app, *, excluded: tuple[str, ...] = ("healthz", "readyz")) -> None:
    """FastAPI：每個請求一個 server span（健康檢查不記）；處理關聯 ID；回應帶上 X-Correlation-ID、X-Trace-ID。"""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    @app.middleware("http")
    async def correlate(request, call_next):
        cid = request.headers.get(CORRELATION_HEADER) or new_correlation_id()  # 客戶端帶了就沿用，沒帶就產生
        with correlated(cid):
            response = await call_next(request)
        response.headers[CORRELATION_HEADER] = cid
        if trace_id := current_trace_id():
            response.headers[TRACE_HEADER] = trace_id
        return response

    FastAPIInstrumentor.instrument_app(app, excluded_urls=",".join(excluded), exclude_spans=["receive", "send"])


def traced_transport(transport, *, peer: str = ""):
    """httpx（Gateway 往後面的服務送請求）：包一層，每個請求一個 client span，header 自動帶 traceparent。

    peer：轉給誰（span 名字會是「POST → ordering」，Jaeger 上一眼看得出這一段要去哪）。
    """
    from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport

    async def name_it(span, request) -> None:
        if peer:
            span.update_name(f"{request.method.decode()} → {peer}")

    return AsyncOpenTelemetryTransport(transport, request_hook=name_it)


# ---------------------------------------------------------------- 關聯 ID
def new_correlation_id() -> str:
    return str(uuid.uuid4())


def correlation_id() -> str:
    """現在這件事的關聯 ID（沒有就是空字串）。"""
    return _correlation.get()


@contextmanager
def correlated(cid: str) -> Iterator[None]:
    """在這一段裡，關聯 ID 是 cid；也記在現在的 span 上。"""
    token = _correlation.set(cid)
    if cid:
        trace.get_current_span().set_attribute(ATTRIBUTE, cid)
    try:
        yield
    finally:
        _correlation.reset(token)


def current_trace_id() -> str:
    """現在這條 trace 的編號（32 個十六進位字）；沒有在追蹤就是空字串。"""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else ""


# ---------------------------------------------------------------- Kafka：寫 outbox、relay 送出、consumer 收到
def outgoing() -> dict[str, str]:
    """要跟著這則訊息走的 header：現在的 trace context（traceparent）＋關聯 ID。不在追蹤裡就是空的。"""
    headers: dict[str, str] = {}
    propagate.inject(headers)  # W3C Trace Context：traceparent（有的話還有 tracestate）
    if cid := correlation_id():
        headers[CORRELATION_KEY] = cid
    return headers


@contextmanager
def sending(topic: str, key: str, kind: str, headers: Mapping[str, str]) -> Iterator[dict[str, str]]:
    """relay 送出一則：從 outbox 抄下的 traceparent 接續，開一個 send span；交出要放進 Kafka header 的值。

    outbox 那一列沒有 trace context（寫的時候沒在追蹤）：照原樣送，不另開一條新的 trace。
    """
    parent = propagate.extract(headers)
    if not trace.get_current_span(parent).get_span_context().is_valid:
        yield dict(headers)
        return
    attributes = _attributes(topic, key, kind, headers)
    with tracer.start_as_current_span(f"send {kind}", context=parent, kind=SpanKind.PRODUCER, attributes=attributes):
        out = {name: value for name, value in headers.items() if name not in ("traceparent", "tracestate")}
        propagate.inject(out)  # 換成 send span 的 traceparent：consumer 會接在它後面
        yield out


@contextmanager
def consuming(message: Message, group: str) -> Iterator[None]:
    """consumer 處理一則：從 Kafka header 接續，開一個 process span；處理時寫的 outbox 會接在它後面。丟例外會記在 span 上。"""
    headers = {name: value.decode() for name, value in (message.headers() or [])}
    kind = headers.get("type", "?")
    key = message.key().decode() if message.key() else ""
    attributes = {
        **_attributes(message.topic(), key, kind, headers),
        "messaging.consumer.group.name": group,
        "messaging.kafka.offset": message.offset(),
        "messaging.destination.partition.id": str(message.partition()),
    }
    with (
        tracer.start_as_current_span(
            f"{group} process {kind}", context=propagate.extract(headers), kind=SpanKind.CONSUMER, attributes=attributes
        ),
        correlated(headers.get(CORRELATION_KEY, "")),
    ):
        yield


def _attributes(topic: str, key: str, kind: str, headers: Mapping[str, str]) -> dict[str, str]:
    attributes = {
        "messaging.system": "kafka",
        "messaging.destination.name": topic,
        "messaging.kafka.message.key": key,
        "foodgo.message.type": kind,
        "foodgo.order_id": key,  # key＝order_id：Jaeger 可以用 foodgo.order_id=A001 搜尋
        ATTRIBUTE: headers.get(CORRELATION_KEY, ""),
    }
    return {name: value for name, value in attributes.items() if value}
