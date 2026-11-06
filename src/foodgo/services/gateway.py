"""API Gateway（第 10 集）：對外唯一的入口（:8000）。看路徑開頭轉給後面的服務，自己不做任何業務：

    uv run python -m foodgo.services.gateway         # http://127.0.0.1:8000

  /orders、/orders/…、/track/…  →  點餐服務（:8101）
  /tickets、/tickets/…          →  廚房服務（:8103）
付款、外送沒有對外的 API：顧客不直接找它們，它們只聽 Kafka。

每個請求一個關聯 ID（X-Correlation-ID）：客戶端帶了就沿用，沒帶就產生；轉給後面的服務、也寫在回應的 header 裡。
追蹤：Gateway 收到請求是 trace 的第一段（server span），轉出去是第二段（httpx 的 client span，自動帶 traceparent）。
後面的服務在哪：FOODGO_ORDERING_URL、FOODGO_KITCHEN_URL（預設 http://127.0.0.1:<埠>）。
"""
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from foodgo.infra import config, service, telemetry

NAME = "gateway"
ROUTES = {"/orders": "ordering", "/track": "ordering", "/tickets": "kitchen"}  # 路徑開頭 → 哪個服務
UPSTREAMS = ("ordering", "payment", "kitchen", "delivery")  # 認得的服務；/services 會問它們的健康檢查
METHODS = ("GET", "POST")  # FoodGo 對外的 API 只用到這兩個
# 只在「這一段連線」有意義的 header，不往下傳；traceparent 由 httpx 的 instrument 換成 Gateway 這一段的
HOP_BY_HOP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "upgrade", "content-length", "traceparent", "tracestate"}
NOT_BACK = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "date", "server"}


def create_app(
    upstreams: Mapping[str, str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 5.0,
) -> FastAPI:
    """upstreams：服務名 → 網址（不給就看 config.service_url）；transport：測試時換成假的後端。"""
    urls = dict(upstreams) if upstreams else {name: config.service_url(name) for name in UPSTREAMS}
    wire = transport or httpx.AsyncHTTPTransport()
    clients = {  # 轉出去的每個請求一個 client span（「POST → ordering」），header 自動帶 traceparent
        name: httpx.AsyncClient(base_url=url, timeout=timeout, transport=telemetry.traced_transport(wire, peer=name))
        for name, url in urls.items()
    }
    probes = {name: httpx.AsyncClient(base_url=url, timeout=2.0, transport=wire) for name, url in urls.items()}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        print(f"{NAME}：" + "、".join(f"{prefix} → {urls[name]}" for prefix, name in ROUTES.items()), flush=True)
        yield
        for client in (*clients.values(), *probes.values()):
            await client.aclose()
        print(f"{NAME}：服務停了", flush=True)

    app = FastAPI(title="好食快送 FoodGo：API Gateway", version=service.VERSION, lifespan=lifespan)

    async def forward(request: Request, to: str) -> Response:
        """原封不動轉過去：方法、路徑、查詢字串、body、header（換掉只屬於這一段連線的）。回來的也原封不動交回去。"""
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["x-correlation-id"] = telemetry.correlation_id()  # 客戶端帶的、或 Gateway 剛產生的
        try:
            upstream = await clients[to].request(
                request.method,
                request.url.path,
                params=request.query_params,
                headers=headers,
                content=await request.body(),
            )
        except httpx.TimeoutException:
            return JSONResponse({"detail": f"{to} 服務沒有在 {timeout:g} 秒內回應"}, status_code=504)
        except httpx.TransportError:
            return JSONResponse({"detail": f"{to} 服務連不上（{urls[to]}）"}, status_code=502)
        back = {k: v for k, v in upstream.headers.items() if k.lower() not in NOT_BACK}
        return Response(upstream.content, status_code=upstream.status_code, headers=back)

    def to(name: str):
        async def route(request: Request) -> Response:
            return await forward(request, name)

        return route

    for prefix, name in ROUTES.items():
        for path in (prefix, prefix + "/{rest:path}"):
            for method in METHODS:
                app.add_api_route(
                    path,
                    to(name),
                    methods=[method],
                    tags=["轉發"],
                    summary=f"轉給 {name} 服務",
                    operation_id=f"{method.lower()}_{path.strip('/').replace('/{rest:path}', '_rest')}",
                )

    @app.get("/healthz", tags=["健康檢查"], summary="Gateway 自己活著嗎（不問後面的服務）")
    def healthz() -> dict:
        return {"service": NAME, "status": "ok"}

    @app.get("/services", tags=["健康檢查"], summary="後面四個服務的 /healthz（不記 trace）")
    async def services() -> dict:
        answers = {}
        for name, probe in probes.items():
            try:
                response = await probe.get("/healthz")
                answers[name] = {"url": urls[name], "http": response.status_code, **response.json()}
            except (httpx.TransportError, ValueError) as error:
                answers[name] = {"url": urls[name], "http": 0, "status": "unreachable", "error": type(error).__name__}
        return answers

    telemetry.instrument(app, excluded=("healthz", "services"))
    return app


def main() -> None:
    service.serve(NAME, create_app)


if __name__ == "__main__":
    main()
