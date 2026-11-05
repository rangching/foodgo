"""組裝點（composition root）：把各上下文的零件接起來。只有這裡可以認識所有上下文。"""
import json
import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from foodgo import contracts
from foodgo.kitchen import api as kitchen_api
from foodgo.kitchen.service import Kitchen
from foodgo.ordering import api as ordering_api
from foodgo.ordering import integration as ordering_integration
from foodgo.ordering.repository import InMemoryOrderRepository, OrderRepository
from foodgo.shared.bus import EventBus
from foodgo.shared.events import DomainEvent


def create_app(
    orders: OrderRepository | None = None,
    publish: Callable[[DomainEvent], None] | None = None,
    *,
    bus: EventBus | None = None,
    kitchen: Kitchen | None = None,
) -> FastAPI:
    bus = bus or EventBus()
    kitchen = kitchen or Kitchen(bus.publish, fault=os.environ.get("FOODGO_FAULT_KITCHEN", ""))

    # 誰訂閱什麼，寫在組裝點：點餐不知道廚房在聽，廚房也不知道是誰發的
    bus.subscribe(contracts.OrderPlaced, kitchen.on_order_placed)

    publish = publish or ordering_integration.publisher(bus.publish)

    app = FastAPI(title="好食快送 FoodGo", version="0.3.0")
    app.include_router(ordering_api.build_router(orders or InMemoryOrderRepository(), publish))
    app.include_router(kitchen_api.build_router(kitchen))

    @app.get("/bus/dead-letters", tags=["事件匯流排"], summary="沒送到的事件")
    def dead_letters() -> list[dict[str, Any]]:
        return [
            {"handler": d.handler, "error": d.error, "message": json.loads(d.message)}
            for d in bus.dead_letters
        ]

    return app


app = create_app()
