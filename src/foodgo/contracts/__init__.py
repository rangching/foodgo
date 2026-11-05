"""共同語言（Published Language）：上下文之間互相傳的整合事件。只用標準函式庫，不認識任何上下文。"""
from foodgo.contracts.base import IntegrationEvent
from foodgo.contracts.kitchen import MealReady
from foodgo.contracts.ordering import OrderItem, OrderPlaced

__all__ = ["IntegrationEvent", "MealReady", "OrderItem", "OrderPlaced"]
