"""點餐的 HTTP 入口：把請求翻成模型的方法呼叫，再把結果翻回 HTTP。規則不寫在這裡。"""
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from foodgo.ordering import codec
from foodgo.ordering.fold import OrderState
from foodgo.ordering.model import Address, Money, Order, OrderRuleViolation
from foodgo.ordering.repository import OrderHistory, OrderRepository
from foodgo.shared.event_store import ConcurrencyConflict

TAIPEI = timezone(timedelta(hours=8))


class LineIn(BaseModel):
    name: str = Field(min_length=1)
    unit_price: int = Field(ge=0, description="單價，元")
    quantity: int = Field(1, ge=1)


class PlaceOrderIn(BaseModel):
    customer_id: str
    restaurant_id: str
    lines: list[LineIn]
    delivery_fee: int = Field(ge=0, description="外送費，元")
    note: str = ""
    delivery_address: str = Field(min_length=1)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
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
            ]
        }
    }


class CancelIn(BaseModel):
    reason: str


class LineOut(BaseModel):
    name: str
    unit_price: int
    quantity: int
    subtotal: int


class OrderOut(BaseModel):
    order_id: str
    status: str
    version: int  # 第幾版：套用了幾個事件（第 6 集）
    customer_id: str
    restaurant_id: str
    lines: list[LineOut]
    subtotal: int
    delivery_fee: int
    total: int
    note: str
    delivery_address: str

    @classmethod
    def of(cls, order: Order | OrderState) -> "OrderOut":
        """Order（現在的訂單）或 OrderState（時光機算出來的那一刻）都可以。"""
        return cls(
            order_id=order.order_id,
            status=order.status,
            version=order.version,
            customer_id=order.customer_id,
            restaurant_id=order.restaurant_id,
            lines=[
                LineOut(
                    name=line.name,
                    unit_price=line.unit_price.amount,
                    quantity=line.quantity,
                    subtotal=line.subtotal.amount,
                )
                for line in order.lines
            ],
            subtotal=sum(line.subtotal.amount for line in order.lines),
            delivery_fee=order.delivery_fee.amount,
            total=order.total.amount,
            note=order.note,
            delivery_address=order.delivery_address.street,
        )


class EventOut(BaseModel):
    version: int
    type: str
    occurred_at: datetime
    data: dict[str, Any]


def build_router(orders: OrderRepository) -> APIRouter:
    router = APIRouter(prefix="/orders", tags=["點餐"])

    def find(order_id: str) -> Order:
        order = orders.get(order_id)
        if order is None:
            raise HTTPException(404, f"找不到訂單 {order_id}")
        return order

    def save(order: Order) -> None:
        try:
            orders.save(order)  # 存訂單，事件也一起交出去
        except ConcurrencyConflict as e:
            raise HTTPException(409, str(e))  # 讀完到寫之間有人先改了：重新讀取、再決定要不要做

    def history() -> OrderHistory:
        if not isinstance(orders, OrderHistory):
            raise HTTPException(501, "這個 Repository 只記現在的樣子，沒有事件可以查（第 6 集起 PostgreSQL 版才有）")
        return orders

    @router.post("", status_code=201, summary="送出訂單")
    def place_order(body: PlaceOrderIn) -> OrderOut:
        order = Order(
            order_id=orders.next_id(),
            customer_id=body.customer_id,
            restaurant_id=body.restaurant_id,
            delivery_address=Address(body.delivery_address),
            delivery_fee=Money(body.delivery_fee),
            note=body.note,
        )
        try:
            for line in body.lines:
                order.add_line(line.name, Money(line.unit_price), line.quantity)
            order.place()
        except OrderRuleViolation as e:
            raise HTTPException(422, str(e))  # 這張訂單本身不成立
        save(order)
        return OrderOut.of(order)

    @router.get("/{order_id}", summary="查一張訂單（as_of：那個時間點的樣子）")
    def get_order(order_id: str, as_of: datetime | None = None) -> OrderOut:
        if as_of is None:
            return OrderOut.of(find(order_id))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=TAIPEI)  # 沒寫時區就當臺北時間
        state = history().load_as_of(order_id, as_of)
        if state is None:
            raise HTTPException(404, f"{as_of:%Y-%m-%d %H:%M:%S%z} 時還沒有訂單 {order_id}")
        return OrderOut.of(state)

    @router.get("/{order_id}/events", summary="這張訂單發生過的每一件事")
    def get_events(order_id: str) -> list[EventOut]:
        records = history().history(order_id)
        if not records:
            raise HTTPException(404, f"找不到訂單 {order_id}")
        return [
            EventOut(
                version=r.version,
                type=r.type,
                occurred_at=r.occurred_at.astimezone(TAIPEI),
                data=codec.to_data(r.event)[1],
            )
            for r in records
        ]

    @router.post("/{order_id}/cancel", summary="取消訂單")
    def cancel_order(order_id: str, body: CancelIn) -> OrderOut:
        order = find(order_id)
        try:
            order.cancel(body.reason)
        except OrderRuleViolation as e:
            raise HTTPException(409, str(e))  # 和訂單目前的狀態衝突
        save(order)
        return OrderOut.of(order)

    return router
