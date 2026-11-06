"""第 10 集：分散式追蹤——trace context 怎麼跟著 HTTP 與 Kafka 走；關聯 ID 怎麼一路傳下去。

執行：uv run pytest tests/ep10 -q（這個檔不用 Docker：span 收在記憶體裡，Kafka 訊息是假的）
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from foodgo import contracts
from foodgo.infra import telemetry


def trace_of(span) -> str:
    return format(span.context.trace_id, "032x")


def traceparent(span) -> str:
    """W3C Trace Context：版本-trace 編號-span 編號-旗標（03＝有取樣＋trace 編號是隨機的）。"""
    return f"00-{trace_of(span)}-{format(span.context.span_id, '016x')}-{int(span.context.trace_flags):02x}"


PLACED = contracts.OrderPlaced(
    order_id="A001",
    restaurant_id="R007",
    items=(contracts.OrderItem(name="牛肉麵", quantity=1), contracts.OrderItem(name="紅茶", quantity=1)),
    note="不要香菜",
)


# ---------------------------------------------------------------- 寫 outbox 時抄下來的 header
def test_outside_any_trace_nothing_extra_is_written(spans):
    assert telemetry.outgoing() == {}  # 第 4～9 集的程序：outbox 的 headers 是 {}，Kafka header 照舊只有 event_id、type
    assert telemetry.correlation_id() == ""
    assert telemetry.current_trace_id() == ""


def test_inside_a_span_the_trace_context_is_copied(spans):
    with telemetry.tracer.start_as_current_span("POST /orders"):
        headers = telemetry.outgoing()
    [span] = spans()
    assert headers == {"traceparent": traceparent(span)}  # 抄下來的就是「現在這一段」


def test_the_correlation_id_goes_along(spans):
    with telemetry.tracer.start_as_current_span("POST /orders"), telemetry.correlated("c-42"):
        assert telemetry.correlation_id() == "c-42"
        headers = telemetry.outgoing()
    assert headers["correlation_id"] == "c-42"
    [span] = spans()
    assert span.attributes["foodgo.correlation_id"] == "c-42"  # Jaeger 上可以用這個 tag 搜尋
    assert telemetry.correlation_id() == ""  # 離開那一段就沒了


def test_trace_ids_are_32_hex_digits(spans):
    with telemetry.tracer.start_as_current_span("x"):
        trace_id = telemetry.current_trace_id()
    assert len(trace_id) == 32 and int(trace_id, 16)


# ---------------------------------------------------------------- relay 送出：接續、換成 send span
def test_sending_continues_the_trace_and_hands_over_its_own_traceparent(spans):
    with telemetry.tracer.start_as_current_span("POST /orders"):
        stored = {**telemetry.outgoing(), "correlation_id": "c-42"}  # outbox 那一列存的
    with telemetry.sending("ordering.events", "A001", "OrderPlaced", {"event_id": "e-1", "type": "OrderPlaced", **stored}) as headers:
        pass
    request, send = spans()
    assert send.name == "send OrderPlaced"
    assert send.kind.name == "PRODUCER"
    assert trace_of(send) == trace_of(request)  # 同一條 trace
    assert send.parent.span_id == request.context.span_id  # 接在寫 outbox 的那一段後面
    assert headers == {"event_id": "e-1", "type": "OrderPlaced", "correlation_id": "c-42", "traceparent": traceparent(send)}
    assert send.attributes["messaging.destination.name"] == "ordering.events"
    assert send.attributes["foodgo.order_id"] == "A001"


def test_a_row_without_trace_context_is_sent_as_is(spans):
    with telemetry.sending("ordering.events", "A001", "OrderPlaced", {"event_id": "e-1", "type": "OrderPlaced"}) as headers:
        pass
    assert headers == {"event_id": "e-1", "type": "OrderPlaced"}
    assert spans() == ()  # 不另開一條沒頭沒尾的 trace


# ---------------------------------------------------------------- consumer 收到：接續、關聯 ID 跟著進來
def test_consuming_continues_the_trace_from_kafka_headers(spans, header_message):
    with telemetry.tracer.start_as_current_span("send OrderPlaced"):
        sent = telemetry.outgoing()
    message = header_message(PLACED, headers={**sent, "correlation_id": "c-42"})
    with telemetry.consuming(message, "saga"):
        assert telemetry.correlation_id() == "c-42"
        nested = telemetry.outgoing()  # 處理時寫 outbox（例如送 AuthorizePayment）
    send, process = spans()
    assert process.name == "saga process OrderPlaced"
    assert process.kind.name == "CONSUMER"
    assert (trace_of(process), process.parent.span_id) == (trace_of(send), send.context.span_id)
    assert nested == {"traceparent": traceparent(process), "correlation_id": "c-42"}  # 下一段接在 process 後面
    attributes = process.attributes
    assert attributes["messaging.consumer.group.name"] == "saga"
    assert attributes["foodgo.message.type"] == "OrderPlaced"
    assert attributes["foodgo.order_id"] == "A001"
    assert attributes["foodgo.correlation_id"] == "c-42"


def test_a_message_without_trace_context_starts_a_new_trace(spans, header_message):
    with telemetry.consuming(header_message(PLACED), "kitchen"):
        pass
    [process] = spans()
    assert process.parent is None  # 第 9 集以前送的訊息：沒有 traceparent，從這裡開始一條新的
    assert "foodgo.correlation_id" not in process.attributes


def test_a_failure_is_recorded_on_the_span(spans, header_message):
    with pytest.raises(RuntimeError), telemetry.consuming(header_message(PLACED), "kitchen"):
        raise RuntimeError("廚房當機")
    [process] = spans()
    assert process.status.status_code.name == "ERROR"  # Jaeger 上這一段是紅的
    assert process.events[0].name == "exception"


# ---------------------------------------------------------------- HTTP：FastAPI 與 httpx 自動帶
def test_a_service_continues_the_trace_from_the_traceparent_header(spans):
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict:
        return {"correlation_id": telemetry.correlation_id(), "outgoing": telemetry.outgoing()}

    telemetry.instrument(app)
    with telemetry.tracer.start_as_current_span("gateway"):
        upstream = telemetry.outgoing()["traceparent"]
    response = TestClient(app).get("/ping", headers={"traceparent": upstream, "X-Correlation-ID": "c-42"})
    body = response.json()
    assert body["correlation_id"] == "c-42"
    assert response.headers["x-correlation-id"] == "c-42"
    gateway_span, server = spans()
    assert server.name == "GET /ping" and server.kind.name == "SERVER"
    assert trace_of(server) == trace_of(gateway_span) == response.headers["x-trace-id"]
    assert body["outgoing"]["traceparent"] == traceparent(server)


def test_no_correlation_id_means_a_new_one(spans):
    app = FastAPI()
    app.get("/ping")(lambda: {"correlation_id": telemetry.correlation_id()})
    telemetry.instrument(app)
    response = TestClient(app).get("/ping")
    assert response.json()["correlation_id"] == response.headers["x-correlation-id"]
    assert len(response.headers["x-correlation-id"]) == 36  # uuid4


def test_health_checks_are_not_traced(spans):
    app = FastAPI()
    app.get("/healthz")(lambda: {"status": "ok"})
    telemetry.instrument(app)
    TestClient(app).get("/healthz")
    assert spans() == ()  # 第 11、12 集每幾秒問一次：不要把 Jaeger 塞滿


def test_httpx_carries_the_traceparent(spans):
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("traceparent"))
        return httpx.Response(200)

    async def call() -> None:
        async with httpx.AsyncClient(transport=telemetry.traced_transport(httpx.MockTransport(upstream))) as client:
            with telemetry.tracer.start_as_current_span("gateway"):
                await client.get("http://ordering/orders/A001")

    import asyncio

    asyncio.run(call())
    client_span, gateway_span = spans()
    assert client_span.kind.name == "CLIENT" and client_span.parent.span_id == gateway_span.context.span_id
    assert seen == [traceparent(client_span)]  # 後面的服務拿到的是 client span 的編號
