"""讀取端的 HTTP 入口：查訂單追蹤（JSON）、追蹤頁（HTML，每秒查一次 JSON）。

只讀 order_tracking：不去問點餐「這張單存不存在」，也不 JOIN 任何上下文的表。
投影還沒讀到就是 404——剛下單時一定會遇到（最終一致）；畫面自己處理：顯示「更新中…」、一秒後再查。
"""
import html
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from foodgo.tracking.page import PAGE
from foodgo.tracking.projection import STATUS, Tracking, headline, steps
from foodgo.tracking.repository import TrackingRepository

TAIPEI = timezone(timedelta(hours=8))


class ItemOut(BaseModel):
    name: str
    quantity: int


class StepOut(BaseModel):
    key: str  # ordering／payment／kitchen／delivery
    title: str
    state: str  # done／active／failed／undone／todo／skipped
    detail: str


class TrackingOut(BaseModel):
    order_id: str
    status: str
    status_label: str
    headline: str
    steps: list[StepOut]
    restaurant_id: str
    items: list[ItemOut]
    note: str
    amount: int | None
    delivery_address: str
    payment: str
    payment_id: str
    refunded: int | None
    kitchen: str
    ticket_id: str
    delivery: str
    delivery_id: str
    courier_id: str
    cancelled: str
    attention: str
    placed_at: datetime | None
    updated_at: datetime  # 最晚那一則事件發生的時間：畫面上的「資料時間」
    as_of_position: int  # 投影一共處理了幾則訊息：這份資料「新到哪裡」

    @classmethod
    def of(cls, t: Tracking, as_of_position: int) -> "TrackingOut":
        return cls(
            order_id=t.order_id,
            status=t.status,
            status_label=STATUS[t.status],
            headline=headline(t),
            steps=[StepOut(key=s.key, title=s.title, state=s.state, detail=s.detail) for s in steps(t)],
            restaurant_id=t.restaurant_id,
            items=[ItemOut(name=i.name, quantity=i.quantity) for i in t.items],
            note=t.note,
            amount=t.amount,
            delivery_address=t.delivery_address,
            payment=t.payment,
            payment_id=t.payment_id,
            refunded=t.refunded,
            kitchen=t.kitchen,
            ticket_id=t.ticket_id,
            delivery=t.delivery,
            delivery_id=t.delivery_id,
            courier_id=t.courier_id,
            cancelled=t.cancelled,
            attention=t.attention,
            placed_at=t.placed_at.astimezone(TAIPEI) if t.placed_at else None,
            updated_at=t.updated_at.astimezone(TAIPEI) if t.updated_at else datetime.now(TAIPEI),
            as_of_position=as_of_position,
        )


def build_router(tracking: TrackingRepository) -> APIRouter:
    router = APIRouter(tags=["訂單追蹤（讀取端）"])

    @router.get(
        "/orders/{order_id}/tracking",
        response_model=TrackingOut,
        summary="訂單追蹤：一次查完四個上下文的進度（讀模型）",
        responses={404: {"description": "投影還沒讀到這張訂單（剛下單時會遇到）：等一下再查"}},
    )
    def get_tracking(order_id: str) -> TrackingOut | JSONResponse:
        row = tracking.get(order_id)
        if row is None:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"還沒有 {order_id} 的追蹤資料：剛下單的話，投影還沒讀到，等一下再查",
                    "as_of_position": tracking.as_of_position(),
                },
                headers={"Retry-After": "1"},
            )
        return TrackingOut.of(row, tracking.as_of_position())

    @router.get("/track/{order_id}", response_class=HTMLResponse, summary="追蹤頁（每秒查一次 tracking API）")
    def tracking_page(order_id: str) -> HTMLResponse:
        return HTMLResponse(PAGE.replace("{{ORDER_ID}}", html.escape(order_id)))

    return router
