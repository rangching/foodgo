"""第 3 集：行程內事件匯流排——發布者不知道誰在聽，訂閱者出錯不會傳回發布者。

執行：uv run pytest tests/ep03 -q
"""
import json
import subprocess
import sys

from foodgo import contracts
from foodgo.shared.bus import EventBus


def meal_ready() -> contracts.MealReady:
    return contracts.MealReady(order_id="A001", ticket_id="T001", restaurant_id="R007")


def test_every_subscriber_gets_the_event():
    bus = EventBus()
    first, second = [], []
    bus.subscribe(contracts.MealReady, first.append)
    bus.subscribe(contracts.MealReady, second.append)
    event = meal_ready()
    bus.publish(event)
    assert first == second == [event]


def test_publishing_with_no_subscribers_is_fine():
    bus = EventBus()
    bus.publish(meal_ready())
    assert bus.dead_letters == []


def test_subscribers_only_get_what_they_asked_for():
    bus = EventBus()
    got = []
    bus.subscribe(contracts.OrderPlaced, got.append)
    bus.publish(meal_ready())
    assert got == []


def test_each_subscriber_gets_a_copy_rebuilt_from_json():
    bus = EventBus()
    got = []
    bus.subscribe(contracts.MealReady, got.append)
    sent = meal_ready()
    bus.publish(sent)
    assert got[0] == sent
    assert got[0] is not sent  # 傳的是資料，不是同一個物件


def test_a_failing_subscriber_does_not_reach_the_publisher():
    bus = EventBus()
    got = []

    def broken(event):
        raise RuntimeError("廚房系統當機")

    bus.subscribe(contracts.MealReady, broken)
    bus.subscribe(contracts.MealReady, got.append)
    event = meal_ready()
    bus.publish(event)  # 不會丟例外

    assert got == [event]  # 排在後面的訂閱者照常收到
    [letter] = bus.dead_letters
    assert letter.handler.endswith("broken")
    assert letter.error == "RuntimeError: 廚房系統當機"
    assert json.loads(letter.message)["data"]["order_id"] == "A001"


def test_the_bus_does_not_know_any_context():
    code = (
        "import sys, foodgo.shared.bus; "
        "print(sorted(m for m in sys.modules "
        "if m.startswith('foodgo.') and not m.startswith('foodgo.shared')))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"
