"""共同語言（Published Language）：上下文之間互相傳的整合事件。只用標準函式庫，不認識任何上下文。"""
from foodgo.contracts.base import IntegrationEvent
from foodgo.contracts.delivery import DELIVERY_TOPIC, CourierAssigned
from foodgo.contracts.kitchen import KITCHEN_TOPIC, MealReady, TicketAccepted, TicketRejected
from foodgo.contracts.ordering import ORDERING_TOPIC, DeliveryRequested, OrderItem, OrderPlaced, PaymentRequested
from foodgo.contracts.payment import PAYMENT_TOPIC, PaymentAuthorized, PaymentFailed

__all__ = [
    "DELIVERY_TOPIC",
    "KITCHEN_TOPIC",
    "ORDERING_TOPIC",
    "PAYMENT_TOPIC",
    "CourierAssigned",
    "DeliveryRequested",
    "IntegrationEvent",
    "MealReady",
    "OrderItem",
    "OrderPlaced",
    "PaymentAuthorized",
    "PaymentFailed",
    "PaymentRequested",
    "TicketAccepted",
    "TicketRejected",
]
