"""組裝點（composition root）：把各上下文的零件接起來。只有這裡可以認識所有上下文。"""
from collections.abc import Callable

from fastapi import FastAPI

from foodgo.ordering import api as ordering_api
from foodgo.ordering.repository import InMemoryOrderRepository, OrderRepository
from foodgo.shared.events import DomainEvent


def print_event(event: DomainEvent) -> None:
    """這一集還沒有人訂閱事件，先印出來看。第 3 集換成事件匯流排。"""
    print(f"領域事件：{type(event).__name__} {getattr(event, 'order_id', '')}", flush=True)


def create_app(
    orders: OrderRepository | None = None,
    publish: Callable[[DomainEvent], None] = print_event,
) -> FastAPI:
    app = FastAPI(title="好食快送 FoodGo", version="0.2.0")
    app.include_router(ordering_api.build_router(orders or InMemoryOrderRepository(), publish))
    return app


app = create_app()
