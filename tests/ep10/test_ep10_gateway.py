"""第 10 集：API Gateway——對外一個埠，看路徑轉給後面的服務；每個請求一個關聯 ID。

執行：uv run pytest tests/ep10 -q（這個檔不用 Docker：後面的服務是假的 httpx.MockTransport）
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from foodgo.services import gateway

A001 = {
    "customer_id": "C042",
    "restaurant_id": "R007",
    "lines": [
        {"name": "牛肉麵", "unit_price": 160, "quantity": 1},
        {"name": "紅茶", "unit_price": 40, "quantity": 1},
    ],
    "delivery_fee": 30,
    "note": "不要香菜",
    "delivery_address": "信義路五段 7 號",
}

UPSTREAMS = {name: f"http://{name}.test" for name in ("ordering", "payment", "kitchen", "delivery")}


class Backend:
    """假的後面四個服務：記下收到什麼，回一個看得出是誰回的答案。"""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = {"service": request.url.host.removesuffix(".test"), "path": request.url.path}
        if request.url.path == "/healthz":
            body["status"] = "ok"
        return httpx.Response(self.status, json=body, headers={"Retry-After": "1"})


@pytest.fixture
def backend() -> Backend:
    return Backend()


@pytest.fixture
def client(backend) -> TestClient:
    return TestClient(gateway.create_app(UPSTREAMS, transport=httpx.MockTransport(backend)))


# ---------------------------------------------------------------- 轉給誰
def test_the_routing_table():
    assert gateway.ROUTES == {"/orders": "ordering", "/track": "ordering", "/tickets": "kitchen"}
    assert "payment" not in gateway.ROUTES.values() and "delivery" not in gateway.ROUTES.values()  # 顧客不直接找它們


@pytest.mark.parametrize(
    "method, path, service",
    [
        ("POST", "/orders", "ordering"),
        ("GET", "/orders/A001", "ordering"),
        ("GET", "/orders/A001/tracking", "ordering"),
        ("GET", "/orders/A001/events", "ordering"),
        ("POST", "/orders/A001/cancel", "ordering"),
        ("GET", "/track/A001", "ordering"),
        ("GET", "/tickets", "kitchen"),
        ("POST", "/tickets/T001/ready", "kitchen"),
    ],
)
def test_paths_go_to_their_service(client, backend, method, path, service):
    response = client.request(method, path)
    assert response.json() == {"service": service, "path": path}
    [request] = backend.requests
    assert (request.method, str(request.url)) == (method, f"http://{service}.test{path}")


def test_anything_else_is_not_found_at_the_gateway(client, backend):
    assert client.get("/payments").status_code == 404
    assert client.get("/kitchen/tickets").status_code == 404
    assert backend.requests == []  # 沒有轉給任何人


def test_the_request_goes_through_untouched(client, backend):
    client.post("/orders?as_of=2026-11-06T15:00", json=A001, headers={"Accept-Language": "zh-TW"})
    [request] = backend.requests
    assert json.loads(request.content) == A001
    assert request.url.params["as_of"] == "2026-11-06T15:00"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept-language"] == "zh-TW"
    assert request.headers["host"] == "ordering.test"  # 這一段連線自己的 host，不是客戶端連 Gateway 的那個


def test_the_answer_comes_back_untouched():
    backend = Backend(status=404)
    client = TestClient(gateway.create_app(UPSTREAMS, transport=httpx.MockTransport(backend)))
    response = client.get("/orders/A009/tracking")
    assert response.status_code == 404  # 讀模型還沒有：原樣交回去
    assert response.headers["retry-after"] == "1"
    assert response.json()["path"] == "/orders/A009/tracking"


# ---------------------------------------------------------------- 關聯 ID
def test_a_correlation_id_is_made_when_missing(client, backend):
    response = client.post("/orders", json=A001)
    cid = response.headers["x-correlation-id"]
    assert len(cid) == 36  # uuid4
    assert backend.requests[0].headers["x-correlation-id"] == cid  # 往後面傳的是同一個


def test_a_correlation_id_from_the_client_is_kept(client, backend):
    response = client.get("/orders/A001", headers={"X-Correlation-ID": "app-7f3a"})
    assert response.headers["x-correlation-id"] == "app-7f3a"
    assert backend.requests[0].headers.get_list("x-correlation-id") == ["app-7f3a"]  # 只有一個，不會疊兩次


def test_every_request_gets_its_own(client):
    first = client.get("/tickets").headers["x-correlation-id"]
    second = client.get("/tickets").headers["x-correlation-id"]
    assert first != second


# ---------------------------------------------------------------- 後面的服務不在
def test_a_service_that_is_down_is_502():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("連線被拒絕", request=request)

    client = TestClient(gateway.create_app(UPSTREAMS, transport=httpx.MockTransport(down)))
    response = client.get("/tickets")
    assert response.status_code == 502
    assert response.json() == {"detail": "kitchen 服務連不上（http://kitchen.test）"}


def test_a_service_that_is_too_slow_is_504():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("太慢了", request=request)

    client = TestClient(gateway.create_app(UPSTREAMS, transport=httpx.MockTransport(slow)))
    response = client.post("/orders", json=A001)
    assert response.status_code == 504
    assert response.json() == {"detail": "ordering 服務沒有在 5 秒內回應"}


def test_errors_carry_the_correlation_id_too():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("連線被拒絕", request=request)

    client = TestClient(gateway.create_app(UPSTREAMS, transport=httpx.MockTransport(down)))
    response = client.get("/tickets", headers={"X-Correlation-ID": "app-7f3a"})
    assert response.headers["x-correlation-id"] == "app-7f3a"  # 出錯也帶著：客服查紀錄用


# ---------------------------------------------------------------- 健康檢查
def test_the_gateway_health_does_not_ask_anyone(client, backend):
    assert client.get("/healthz").json() == {"service": "gateway", "status": "ok"}
    assert backend.requests == []


def test_services_asks_every_service(client, backend):
    answers = client.get("/services").json()
    assert set(answers) == {"ordering", "payment", "kitchen", "delivery"}
    assert answers["payment"] == {
        "url": "http://payment.test",
        "http": 200,
        "service": "payment",
        "path": "/healthz",
        "status": "ok",
    }
    assert sorted(str(r.url) for r in backend.requests) == sorted(f"{u}/healthz" for u in UPSTREAMS.values())


def test_where_the_services_are_comes_from_the_environment(monkeypatch):
    from foodgo.infra import config

    monkeypatch.delenv("FOODGO_KITCHEN_URL", raising=False)
    monkeypatch.delenv("FOODGO_KITCHEN_PORT", raising=False)
    assert config.service_url("kitchen") == "http://127.0.0.1:8103"
    monkeypatch.setenv("FOODGO_KITCHEN_PORT", "28103")
    assert config.service_url("kitchen") == "http://127.0.0.1:28103"
    monkeypatch.setenv("FOODGO_KITCHEN_URL", "http://kitchen:8000")  # 第 11 集：容器之間用服務名
    assert config.service_url("kitchen") == "http://kitchen:8000"


def test_the_gateway_docs_list_only_the_forwarding_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {
        "/orders",
        "/orders/{rest}",
        "/track",
        "/track/{rest}",
        "/tickets",
        "/tickets/{rest}",
        "/healthz",
        "/services",
    }
