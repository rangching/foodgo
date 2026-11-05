"""廚房的應用服務：開廚房單、出餐。"""
from collections.abc import Callable, Iterable

from foodgo import contracts
from foodgo.kitchen.ticket import Dish, Ticket


class KitchenDown(Exception):
    """廚房系統掛了。用環境變數 FOODGO_FAULT_KITCHEN=crash 模擬。"""


class Kitchen:
    def __init__(
        self,
        publish: Callable[[contracts.IntegrationEvent], None] | None = None,
        *,
        fault: str = "",
    ) -> None:
        self._publish = publish or (lambda message: None)
        self._fault = fault
        self._tickets: dict[str, Ticket] = {}

    def tickets(self) -> list[Ticket]:
        return list(self._tickets.values())

    def get(self, ticket_id: str) -> Ticket | None:
        return self._tickets.get(ticket_id)

    def create_ticket(
        self, order_id: str, restaurant_id: str, dishes: Iterable[Dish], note: str = ""
    ) -> Ticket:
        if self._fault == "crash":
            raise KitchenDown("廚房系統當機，收不了單")
        ticket = Ticket(f"T{len(self._tickets) + 1:03d}", order_id, restaurant_id, tuple(dishes), note)
        self._tickets[ticket.ticket_id] = ticket
        menu = "、".join(f"{d.name} ×{d.quantity}" for d in ticket.dishes)
        print(f"廚房：開廚房單 {ticket.ticket_id}（訂單 {order_id}）{menu}；備註：{note or '無'}", flush=True)
        return ticket

    # 訂閱 OrderPlaced：收到「訂單已送出」就開一張廚房單
    def on_order_placed(self, event: contracts.OrderPlaced) -> None:
        dishes = [Dish(item.name, item.quantity) for item in event.items]
        self.create_ticket(event.order_id, event.restaurant_id, dishes, event.note)

    # 出餐：廚房單標成備妥，對外公告 MealReady（誰要聽，廚房不用知道）
    def mark_ready(self, ticket: Ticket) -> None:
        ticket.mark_ready()
        print(f"廚房：{ticket.ticket_id} 出餐，發布 MealReady {ticket.order_id}", flush=True)
        self._publish(
            contracts.MealReady(
                order_id=ticket.order_id, ticket_id=ticket.ticket_id, restaurant_id=ticket.restaurant_id
            )
        )
