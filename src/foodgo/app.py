"""組裝點（composition root）：把各上下文的零件接起來。只有這裡可以認識所有上下文。"""
import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from foodgo import contracts
from foodgo.infra import config, db
from foodgo.kitchen import api as kitchen_api
from foodgo.kitchen import postgres as kitchen_postgres
from foodgo.kitchen.service import Kitchen
from foodgo.ordering import api as ordering_api
from foodgo.ordering import event_sourcing as ordering_event_sourcing
from foodgo.ordering import integration as ordering_integration
from foodgo.ordering.repository import InMemoryOrderRepository, OrderRepository
from foodgo.shared.bus import EventBus
from foodgo.shared.events import DomainEvent
from foodgo.tracking import api as tracking_api
from foodgo.tracking import postgres as tracking_postgres
from foodgo.tracking.repository import InMemoryTrackingRepository, TrackingRepository


def create_app(
    orders: OrderRepository | None = None,
    publish: Callable[[DomainEvent], None] | None = None,
    *,
    bus: EventBus | None = None,
    kitchen: Kitchen | None = None,
    tracking: TrackingRepository | None = None,
    on_startup: Callable[[], None] | None = None,
) -> FastAPI:
    """什麼都不給：全部放記憶體、用行程內匯流排（第 3 集的樣子，單元測試用這個）。"""
    bus = bus or EventBus()
    kitchen = kitchen or Kitchen(bus.publish, fault=os.environ.get("FOODGO_FAULT_KITCHEN", ""))

    # 誰訂閱什麼，寫在組裝點：點餐不知道廚房在聽，廚房也不知道是誰發的
    bus.subscribe(contracts.OrderPlaced, kitchen.on_order_placed)
    if tracking is None:  # 記憶體版（第 9 集）：投影也只是匯流排上的另一個訂閱者，收到就投影、沒有延遲
        memory = InMemoryTrackingRepository()
        bus.subscribe(contracts.OrderPlaced, memory.on_event)
        bus.subscribe(contracts.MealReady, memory.on_event)
        tracking = memory

    publish = publish or ordering_integration.publisher(bus.publish)
    orders = orders or InMemoryOrderRepository(publish)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if on_startup:
            on_startup()
        yield

    app = FastAPI(title="好食快送 FoodGo", version="0.9.0", lifespan=lifespan)
    app.include_router(ordering_api.build_router(orders))
    app.include_router(kitchen_api.build_router(kitchen))
    app.include_router(tracking_api.build_router(tracking))  # 讀取端：GET /orders/{id}/tracking、追蹤頁 /track/{id}

    @app.get("/bus/dead-letters", tags=["事件匯流排"], summary="沒送到的事件")
    def dead_letters() -> list[dict[str, Any]]:
        return [
            {"handler": d.handler, "error": d.error, "message": json.loads(d.message)}
            for d in bus.dead_letters
        ]

    return app


def create_postgres_app(url: str | None = None) -> FastAPI:
    """第 4 集起：訂單、廚房單存 PostgreSQL。事件寫進 outbox，由 relay 送到 Kafka，廚房 consumer 另外跑。

    第 6 集起訂單改成事件溯源：存在 events 表，要用的時候 fold 回來（第 4 集的 orders 表不再用）。
    第 7 集起付款、廚房、外送各自是一個 consumer，聽事件接力（編舞式 Saga）；API 只負責下單與廚房出餐。
    第 8 集起由編排器（foodgo.saga.consumer）送命令指揮；API 這邊不變，取消訂單時多公告一則 OrderCancelled。
    第 9 集起多了讀取端：追蹤 API 只讀 order_tracking（由 foodgo.tracking.consumer 從事件投影出來）。
    """
    url = url or config.database_url()
    return create_app(
        orders=ordering_event_sourcing.EventSourcedOrderRepository(url),
        kitchen=Kitchen(tickets=kitchen_postgres.PostgresTicketRepository(url)),
        tracking=tracking_postgres.PostgresTrackingRepository(url),
        on_startup=lambda: db.create_tables(
            url, *ordering_event_sourcing.SCHEMAS, *kitchen_postgres.SCHEMAS, *tracking_postgres.SCHEMAS
        ),
    )


app = create_postgres_app()  # uv run fastapi dev 跑的是這個；import 時還不會連資料庫，啟動時才建表
