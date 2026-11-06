"""第 9 集：查詢 API 與追蹤頁——一次查完；還沒投影到就 404，畫面自己顯示「更新中…」。

執行：uv run pytest tests/ep09 -q（這個檔不用 Docker：用記憶體版的讀模型）
"""
import pytest
from ep09_facts import happy, refund_failed, rejected
from fastapi.testclient import TestClient

from foodgo.app import create_app
from foodgo.tracking.repository import InMemoryTrackingRepository

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


def fed(events) -> TestClient:
    """一個讀模型，先餵好這些事件（像投影 consumer 已經讀完了）。"""
    tracking = InMemoryTrackingRepository()
    for event in events:
        tracking.on_event(event)
    return TestClient(create_app(tracking=tracking))


def test_one_request_answers_everything():
    body = fed(happy()).get("/orders/A001/tracking").json()
    assert (body["order_id"], body["status"], body["status_label"]) == ("A001", "delivering", "外送中")
    assert [(s["key"], s["state"]) for s in body["steps"]] == [
        ("ordering", "done"),
        ("payment", "done"),
        ("kitchen", "done"),
        ("delivery", "done"),
    ]
    assert body["items"] == [{"name": "牛肉麵", "quantity": 1}, {"name": "紅茶", "quantity": 1}]
    assert (body["amount"], body["delivery_address"], body["courier_id"]) == (230, "信義路五段 7 號", "M01")
    assert body["updated_at"] == "2026-11-06T14:50:03+08:00"  # 臺北時間
    assert body["as_of_position"] == 6  # 讀模型處理了幾則


def test_not_projected_yet_is_404_with_a_hint():
    response = fed(happy("A001")).get("/orders/A002/tracking")
    assert response.status_code == 404
    assert response.headers["retry-after"] == "1"  # 一秒後再來
    body = response.json()
    assert body["detail"] == "還沒有 A002 的追蹤資料：剛下單的話，投影還沒讀到，等一下再查"
    assert body["as_of_position"] == 6


def test_a_rejected_order():
    body = fed(rejected()).get("/orders/A001/tracking").json()
    assert (body["status"], body["status_label"]) == ("cancelled", "已取消")
    assert [(s["state"], s["detail"]) for s in body["steps"]] == [
        ("undone", "已取消"),
        ("undone", "已退款 230 元"),
        ("failed", "餐廳拒單：餐廳太忙，暫停接單"),
        ("skipped", "不用送了"),
    ]
    assert (body["refunded"], body["cancelled"]) == (230, "餐廳拒單：餐廳太忙，暫停接單")


def test_needs_attention():
    body = fed(refund_failed()).get("/orders/A001/tracking").json()
    assert (body["status"], body["status_label"]) == ("needs_attention", "人工處理中")
    assert body["attention"] == "RefundPayment 試了 2 次都失敗（發卡銀行暫時無法退款）"


def test_in_memory_the_projection_is_just_another_subscriber():
    """第 3 集的匯流排模式：同一個行程、同步投影，下單完馬上查得到（只有 OrderPlaced：沒有付款、廚房的 consumer）。"""
    client = TestClient(create_app())
    assert client.post("/orders", json=A001).status_code == 201
    body = client.get("/orders/A001/tracking").json()
    assert (body["status"], body["amount"], body["delivery_address"]) == ("placed", None, "")  # 只聽到 OrderPlaced
    assert [s["state"] for s in body["steps"]] == ["done", "active", "todo", "todo"]


@pytest.mark.parametrize(
    "color", ["#6fb3f2", "#5fd38d", "#ef7b6b", "#c89cf2"], ids=["ordering", "payment", "kitchen", "delivery"]
)
def test_the_page_uses_the_series_colors(color):
    page = fed(happy()).get("/track/A001")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert color in page.text


def test_the_page_polls_the_tracking_api_and_handles_404():
    page = fed(happy()).get("/track/A001").text
    assert "<title>A001 訂單追蹤｜好食快送 FoodGo</title>" in page
    assert "/tracking`" in page and "setTimeout(poll, 1000)" in page  # 每秒查一次
    assert "response.status === 404" in page and "更新中…" in page  # 還沒投影到：畫面自己處理


def test_the_page_escapes_the_order_id():
    page = fed(happy()).get("/track/<b>x").text
    assert "<b>x" not in page and "&lt;b&gt;x" in page
