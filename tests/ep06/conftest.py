"""第 6 集的測試工具：A001 的一生，時間固定在 2026-11-06 下午（時光機要查得到「下午 3 點」）。

整合測試的共用工具（foodgo_test 資料庫、臨時 topic、沒開 Docker 自動略過）在 tests/conftest.py。
"""
from datetime import datetime, timedelta, timezone

import pytest

TAIPEI = timezone(timedelta(hours=8))


def at(clock: str) -> datetime:
    """'15:00:00' → 2026-11-06 15:00:00（臺北時間）。"""
    hour, minute, second = map(int, clock.split(":"))
    return datetime(2026, 11, 6, hour, minute, second, tzinfo=TAIPEI)


@pytest.fixture
def a001_afternoon() -> list:
    """A001：14:50 開始點 → 加牛肉麵 → 加紅茶 → 14:51 送出 → 15:20 顧客取消。每個測試拿到新的一份。"""
    from foodgo.ordering.events import ItemAdded, OrderCancelled, OrderDrafted, OrderPlaced
    from foodgo.ordering.model import Address, Money, OrderLine

    return [
        OrderDrafted(
            occurred_at=at("14:50:00"),
            order_id="A001",
            customer_id="C042",
            restaurant_id="R007",
            delivery_address=Address("信義路五段 7 號"),
            delivery_fee=Money(30),
            note="不要香菜",
        ),
        ItemAdded(occurred_at=at("14:50:20"), order_id="A001", name="牛肉麵", unit_price=Money(160), quantity=1),
        ItemAdded(occurred_at=at("14:50:35"), order_id="A001", name="紅茶", unit_price=Money(40), quantity=1),
        OrderPlaced(
            occurred_at=at("14:51:00"),
            order_id="A001",
            customer_id="C042",
            restaurant_id="R007",
            lines=(OrderLine("牛肉麵", Money(160)), OrderLine("紅茶", Money(40))),
            note="不要香菜",
        ),
        OrderCancelled(occurred_at=at("15:20:00"), order_id="A001", reason="顧客改變心意"),
    ]
