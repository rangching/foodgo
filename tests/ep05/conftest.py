"""第 5 集的測試工具：範例訂單 A001 的一生，寫成一串事件。"""
import pytest


@pytest.fixture
def a001_events() -> list:
    """A001：建立草稿 → 加牛肉麵 → 加紅茶 → 送出 → 取消。每個測試拿到新的一份。"""
    from foodgo.ordering.events import ItemAdded, OrderCancelled, OrderDrafted, OrderPlaced  # 起點還沒有，用到才 import
    from foodgo.ordering.model import Address, Money, OrderLine

    return [
        OrderDrafted(
            order_id="A001",
            customer_id="C042",
            restaurant_id="R007",
            delivery_address=Address("信義路五段 7 號"),
            delivery_fee=Money(30),
            note="不要香菜",
        ),
        ItemAdded(order_id="A001", name="牛肉麵", unit_price=Money(160), quantity=1),
        ItemAdded(order_id="A001", name="紅茶", unit_price=Money(40), quantity=1),
        OrderPlaced(
            order_id="A001",
            customer_id="C042",
            restaurant_id="R007",
            lines=(OrderLine("牛肉麵", Money(160)), OrderLine("紅茶", Money(40))),
            note="不要香菜",
        ),
        OrderCancelled(order_id="A001", reason="顧客改變心意"),
    ]
