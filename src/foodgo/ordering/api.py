"""點餐的 HTTP 入口：把請求翻成模型的方法呼叫，再把結果翻回 HTTP。規則不寫在這裡。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from foodgo.ordering.model import Address, Money, Order, OrderRuleViolation
from foodgo.ordering.repository import OrderRepository


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
    customer_id: str
    restaurant_id: str
    lines: list[LineOut]
    subtotal: int
    delivery_fee: int
    total: int
    note: str
    delivery_address: str

    @classmethod
    def of(cls, order: Order) -> "OrderOut":
        return cls(
            order_id=order.order_id,
            status=order.status,
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
            subtotal=order.subtotal.amount,
            delivery_fee=order.delivery_fee.amount,
            total=order.total.amount,
            note=order.note,
            delivery_address=order.delivery_address.street,
        )


def build_router(orders: OrderRepository) -> APIRouter:
    router = APIRouter(prefix="/orders", tags=["點餐"])

    def find(order_id: str) -> Order:
        order = orders.get(order_id)
        if order is None:
            raise HTTPException(404, f"找不到訂單 {order_id}")
        return order

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
        orders.save(order)  # 存訂單，事件也一起交出去
        return OrderOut.of(order)

    @router.get("/{order_id}", summary="查一張訂單")
    def get_order(order_id: str) -> OrderOut:
        return OrderOut.of(find(order_id))

    @router.post("/{order_id}/cancel", summary="取消訂單")
    def cancel_order(order_id: str, body: CancelIn) -> OrderOut:
        order = find(order_id)
        try:
            order.cancel(body.reason)
        except OrderRuleViolation as e:
            raise HTTPException(409, str(e))  # 和訂單目前的狀態衝突
        orders.save(order)
        return OrderOut.of(order)

    return router
