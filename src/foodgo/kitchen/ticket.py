from dataclasses import dataclass


@dataclass
class Ticket:
    """廚房單：廚房只在乎做哪些菜、有什麼備註，不看價格。"""

    ticket_id: str
    order_id: str  # 只記訂單編號，不認識點餐上下文的物件
    dishes: list[str]
    note: str = ""
