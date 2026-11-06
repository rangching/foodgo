"""第 3 集：點餐發布 OrderPlaced，廚房訂閱後開廚房單；廚房掛了，下單照樣成功。

執行：uv run pytest tests/ep03 -q
"""
import pytest
from fastapi.testclient import TestClient

from foodgo import contracts
from foodgo.app import create_app
from foodgo.kitchen.service import Kitchen
from foodgo.ordering.events import OrderCancelled, OrderPlaced
from foodgo.ordering.integration import to_integration
from foodgo.ordering.model import Address, Money, Order
from foodgo.shared.bus import EventBus

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
def bus():
    return EventBus()


# ---------------------------------------------------------------- 點餐：領域事件 → 整合事件
def test_domain_event_keeps_prices_inside_ordering():
    order = Order(
        order_id="A001",
        customer_id="C042",
        restaurant_id="R007",
        delivery_address=Address("信義路五段 7 號"),
        delivery_fee=Money(30),
        note="不要香菜",
    )
    order.add_line("牛肉麵", Money(160))
    order.add_line("紅茶", Money(40))
    order.place()
    *_, event = order.pull_events()  # 最後一個是 OrderPlaced；前面是建立草稿、加品項（第 6 集起）

    assert isinstance(event, OrderPlaced)
    assert [(line.name, line.unit_price, line.quantity) for line in event.lines] == [
        ("牛肉麵", Money(160), 1),
        ("紅茶", Money(40), 1),
    ]
    assert event.note == "不要香菜"

    message = to_integration(event)
    assert isinstance(message, contracts.OrderPlaced)
    assert message.items == (
        contracts.OrderItem(name="牛肉麵", quantity=1),
        contracts.OrderItem(name="紅茶", quantity=1),
    )
    assert (message.order_id, message.restaurant_id, message.note) == ("A001", "R007", "不要香菜")
    assert message.event_id == str(event.event_id)  # 同一件事，同一個編號


def test_cancelled_is_not_announced_yet():
    assert to_integration(OrderCancelled(order_id="A001", reason="顧客改變心意")) is None


# ---------------------------------------------------------------- 廚房：訂閱 OrderPlaced
def test_placing_an_order_opens_a_kitchen_ticket(bus):
    client = TestClient(create_app(bus=bus))
    assert client.post("/orders", json=A001).status_code == 201
    assert client.get("/tickets").json() == [
        {
            "ticket_id": "T001",
            "order_id": "A001",
            "restaurant_id": "R007",
            "dishes": [{"name": "牛肉麵", "quantity": 1}, {"name": "紅茶", "quantity": 1}],
            "note": "不要香菜",
            "status": "received",
        }
    ]


def test_kitchen_failure_does_not_fail_the_order(bus):
    client = TestClient(create_app(bus=bus, kitchen=Kitchen(publish=bus.publish, fault="crash")))
    response = client.post("/orders", json=A001)
    assert response.status_code == 201
    assert response.json()["total"] == 230
    assert client.get("/orders/A001").json()["status"] == "placed"

    assert client.get("/tickets").json() == []  # 廚房沒收到
    [letter] = client.get("/bus/dead-letters").json()
    assert letter["handler"] == "Kitchen.on_order_placed"
    assert letter["error"] == "KitchenDown: 廚房系統當機，收不了單"
    assert letter["message"]["type"] == "OrderPlaced"
    assert letter["message"]["data"]["order_id"] == "A001"


def test_fault_switch_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("FOODGO_FAULT_KITCHEN", "crash")
    client = TestClient(create_app())
    assert client.post("/orders", json=A001).status_code == 201
    assert len(client.get("/bus/dead-letters").json()) == 1


def test_calling_the_kitchen_directly_takes_ordering_down():
    """對照組：不經過匯流排，publish 直接叫廚房——廚房的例外一路傳回下單。"""
    kitchen = Kitchen(fault="crash")

    def call_kitchen_directly(event):
        kitchen.on_order_placed(to_integration(event))

    client = TestClient(create_app(publish=call_kitchen_directly), raise_server_exceptions=False)
    assert client.post("/orders", json=A001).status_code == 500


# ---------------------------------------------------------------- 廚房：出餐公告 MealReady
def test_meal_ready_is_announced_when_the_kitchen_is_done(bus):
    got = []
    bus.subscribe(contracts.MealReady, got.append)
    client = TestClient(create_app(bus=bus))
    client.post("/orders", json=A001)

    response = client.post("/tickets/T001/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert [(e.order_id, e.ticket_id, e.restaurant_id) for e in got] == [("A001", "T001", "R007")]

    assert client.post("/tickets/T001/ready").status_code == 409  # 出過餐了
    assert client.post("/tickets/T999/ready").status_code == 404
    assert len(got) == 1


def test_docs_page_lists_the_kitchen_api(bus):
    paths = TestClient(create_app(bus=bus)).get("/openapi.json").json()["paths"]
    assert {"/tickets", "/tickets/{ticket_id}/ready", "/bus/dead-letters"} <= set(paths)
