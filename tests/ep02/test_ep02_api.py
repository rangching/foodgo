"""第 2 集：FastAPI 下單 API。規則在模型裡，API 只負責把結果翻成 HTTP。

執行：uv run pytest tests/ep02 -q
"""
import pytest
from fastapi.testclient import TestClient

from foodgo.app import create_app
from foodgo.ordering.events import ItemAdded, OrderCancelled, OrderDrafted, OrderPlaced

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


@pytest.fixture
def published():
    return []


@pytest.fixture
def client(published):
    return TestClient(create_app(publish=published.append))


def test_place_order_returns_201_and_230(client):
    response = client.post("/orders", json=A001)
    assert response.status_code == 201
    body = response.json()
    assert body["order_id"] == "A001"
    assert body["status"] == "placed"
    assert [line["subtotal"] for line in body["lines"]] == [160, 40]
    assert body["subtotal"] == 200
    assert body["total"] == 230
    assert body["note"] == "不要香菜"
    assert body["delivery_address"] == "信義路五段 7 號"


def test_get_order_returns_what_was_saved(client):
    created = client.post("/orders", json=A001).json()
    response = client.get("/orders/A001")
    assert response.status_code == 200
    assert response.json() == created


def test_unknown_order_is_404(client):
    assert client.get("/orders/A999").status_code == 404


def test_empty_order_is_rejected_by_the_model(client):
    response = client.post("/orders", json={**A001, "lines": []})
    assert response.status_code == 422
    assert "至少要有一個品項" in response.json()["detail"]


def test_malformed_request_is_422(client):
    bad = {**A001, "lines": [{"name": "紅茶", "unit_price": 40, "quantity": 0}]}
    assert client.post("/orders", json=bad).status_code == 422


def test_cancel_twice_is_409(client):
    client.post("/orders", json=A001)
    first = client.post("/orders/A001/cancel", json={"reason": "顧客改變心意"})
    assert first.status_code == 200
    assert first.json()["status"] == "cancelled"
    second = client.post("/orders/A001/cancel", json={"reason": "再取消一次"})
    assert second.status_code == 409


def test_events_are_published_once(client, published):
    client.post("/orders", json=A001)
    placed = [OrderDrafted, ItemAdded, ItemAdded, OrderPlaced]  # 第 6 集起，每個動作都記一個事件
    assert [type(e) for e in published] == placed
    client.post("/orders/A001/cancel", json={"reason": "顧客改變心意"})
    assert [type(e) for e in published] == [*placed, OrderCancelled]


def test_docs_page_lists_the_order_api(client):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/orders", "/orders/{order_id}", "/orders/{order_id}/cancel"} <= set(paths)
