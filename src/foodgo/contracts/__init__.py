"""共同語言（Published Language）：上下文之間互相傳的整合事件與命令。只用標準函式庫，不認識任何上下文。"""
from foodgo.contracts.base import Command, IntegrationEvent, Message
from foodgo.contracts.delivery import (
    DELIVERY_COMMANDS,
    DELIVERY_TOPIC,
    AssignCourier,
    CourierAssigned,
)
from foodgo.contracts.kitchen import (
    KITCHEN_COMMANDS,
    KITCHEN_TOPIC,
    AcceptTicket,
    CancelTicket,
    MealReady,
    TicketAccepted,
    TicketCancelled,
    TicketRejected,
)
from foodgo.contracts.ordering import (
    ORDERING_COMMANDS,
    ORDERING_TOPIC,
    CancelOrder,
    DeliveryRequested,
    OrderCancelled,
    OrderItem,
    OrderPlaced,
    PaymentRequested,
)
from foodgo.contracts.payment import (
    PAYMENT_COMMANDS,
    PAYMENT_TOPIC,
    AuthorizePayment,
    PaymentAuthorized,
    PaymentFailed,
    PaymentRefunded,
    RefundFailed,
    RefundPayment,
)
from foodgo.contracts.saga import SAGA_TOPIC, SagaEscalated

# 命令送到接收者的命令 topic（第 8 集）：<receiver>.commands
COMMAND_TOPICS = {
    "ordering": ORDERING_COMMANDS,
    "payment": PAYMENT_COMMANDS,
    "kitchen": KITCHEN_COMMANDS,
    "delivery": DELIVERY_COMMANDS,
}

__all__ = [
    "COMMAND_TOPICS",
    "DELIVERY_COMMANDS",
    "DELIVERY_TOPIC",
    "KITCHEN_COMMANDS",
    "KITCHEN_TOPIC",
    "ORDERING_COMMANDS",
    "ORDERING_TOPIC",
    "PAYMENT_COMMANDS",
    "PAYMENT_TOPIC",
    "SAGA_TOPIC",
    "AcceptTicket",
    "AssignCourier",
    "AuthorizePayment",
    "CancelOrder",
    "CancelTicket",
    "Command",
    "CourierAssigned",
    "DeliveryRequested",
    "IntegrationEvent",
    "MealReady",
    "Message",
    "OrderCancelled",
    "OrderItem",
    "OrderPlaced",
    "PaymentAuthorized",
    "PaymentFailed",
    "PaymentRefunded",
    "PaymentRequested",
    "RefundFailed",
    "RefundPayment",
    "SagaEscalated",
    "TicketAccepted",
    "TicketCancelled",
    "TicketRejected",
]
