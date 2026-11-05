"""廚房的 HTTP 入口：看廚房單、出餐。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from foodgo.kitchen.service import Kitchen
from foodgo.kitchen.ticket import Ticket, TicketRuleViolation


class DishOut(BaseModel):
    name: str
    quantity: int


class TicketOut(BaseModel):
    ticket_id: str
    order_id: str
    restaurant_id: str
    dishes: list[DishOut]
    note: str
    status: str

    @classmethod
    def of(cls, ticket: Ticket) -> "TicketOut":
        return cls(
            ticket_id=ticket.ticket_id,
            order_id=ticket.order_id,
            restaurant_id=ticket.restaurant_id,
            dishes=[DishOut(name=d.name, quantity=d.quantity) for d in ticket.dishes],
            note=ticket.note,
            status=ticket.status,
        )


def build_router(kitchen: Kitchen) -> APIRouter:
    router = APIRouter(prefix="/tickets", tags=["廚房"])

    @router.get("", summary="列出廚房單")
    def list_tickets() -> list[TicketOut]:
        return [TicketOut.of(ticket) for ticket in kitchen.tickets()]

    @router.post("/{ticket_id}/ready", summary="出餐")
    def mark_ready(ticket_id: str) -> TicketOut:
        ticket = kitchen.get(ticket_id)
        if ticket is None:
            raise HTTPException(404, f"找不到廚房單 {ticket_id}")
        try:
            kitchen.mark_ready(ticket)
        except TicketRuleViolation as e:
            raise HTTPException(409, str(e))
        return TicketOut.of(ticket)

    return router
