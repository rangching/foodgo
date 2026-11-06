"""第 9 集的測試資料：A001 在各個情境下，四個上下文（和編排器）公告的事實——照第 8 集編排器實際的順序。

時間固定（臺北時間 14:50:00 起，一秒一則），event_id 固定：測試可以比對 updated_at，也可以把同一則送兩次。
測試檔用 from ep09_facts import …（pytest 會把這個資料夾放進 sys.path）。
"""
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

START = datetime(2026, 11, 6, 6, 50, tzinfo=UTC)  # 臺北時間 14:50
BUSY = "餐廳太忙，暫停接單"
NAMESPACE = UUID("00000000-0000-0000-0000-00000000f009")


def _at(second: int) -> str:
    return (START + timedelta(seconds=second)).isoformat(timespec="seconds")


def _make(kind, second: int, order_id: str, *, again: bool = False, **data):
    """一則事件：編號由「哪張單、什麼事件、第幾秒」算出來，每次跑都一樣。

    again：參與者被重送命令時「再回一次」——同一件事、新的 event_id。
    """
    event_id = str(uuid5(NAMESPACE, f"{order_id}/{kind.__name__}/{second}{'/again' if again else ''}"))
    return kind(event_id=event_id, occurred_at=_at(second), order_id=order_id, **data)


def placed(order_id: str = "A001") -> list:
    """點餐送出時公告的三則（第 7 集起）：同一個交易、同一個時間。"""
    from foodgo import contracts

    items = (contracts.OrderItem(name="牛肉麵", quantity=1), contracts.OrderItem(name="紅茶", quantity=1))
    return [
        _make(contracts.OrderPlaced, 0, order_id, restaurant_id="R007", items=items, note="不要香菜"),
        _make(contracts.PaymentRequested, 0, order_id, customer_id="C042", amount=230),
        _make(contracts.DeliveryRequested, 0, order_id, restaurant_id="R007", delivery_address="信義路五段 7 號"),
    ]


def happy(order_id: str = "A001") -> list:
    """一路走完：付款 → 接單 → 指派外送員。"""
    from foodgo import contracts

    return [
        *placed(order_id),
        _make(contracts.PaymentAuthorized, 1, order_id, payment_id="P001"),
        _make(contracts.TicketAccepted, 2, order_id, ticket_id="T001"),
        _make(contracts.CourierAssigned, 3, order_id, delivery_id="D001", courier_id="M01"),
    ]


def declined(order_id: str = "A001") -> list:
    """付款失敗 → 取消訂單。"""
    from foodgo import contracts

    return [
        *placed(order_id),
        _make(contracts.PaymentFailed, 1, order_id, payment_id="P001", reason="卡片額度不足"),
        _make(contracts.OrderCancelled, 2, order_id, reason="付款失敗：卡片額度不足"),
    ]


def rejected(order_id: str = "A001") -> list:
    """付款成功、餐廳拒單 → 退款 → 取消訂單（第 8 集的主畫面）。"""
    from foodgo import contracts

    return [
        *placed(order_id),
        _make(contracts.PaymentAuthorized, 1, order_id, payment_id="P001"),
        _make(contracts.TicketRejected, 2, order_id, ticket_id="T001", reason=BUSY),
        _make(contracts.PaymentRefunded, 3, order_id, payment_id="P001", amount=230),
        _make(contracts.OrderCancelled, 4, order_id, reason=f"餐廳拒單：{BUSY}"),
    ]


def refund_failed(order_id: str = "A001") -> list:
    """拒單、退款試了兩次都失敗 → 編排器放棄、公告 SagaEscalated（第 9 集）。"""
    from foodgo import contracts

    reason = "發卡銀行暫時無法退款"
    return [
        *placed(order_id),
        _make(contracts.PaymentAuthorized, 1, order_id, payment_id="P001"),
        _make(contracts.TicketRejected, 2, order_id, ticket_id="T001", reason=BUSY),
        _make(contracts.RefundFailed, 3, order_id, payment_id="P001", reason=reason),
        _make(contracts.RefundFailed, 4, order_id, payment_id="P001", reason=reason),
        _make(contracts.SagaEscalated, 4, order_id, reason=f"RefundPayment 試了 2 次都失敗（{reason}）"),
    ]


def kitchen_crashed(order_id: str = "A001") -> list:
    """廚房當機：逾時補償（撤銷、退款、取消）之後，修好的廚房才接單、再被撤銷——遲到的「已接單」。"""
    from foodgo import contracts

    reason = "廚房沒有回覆：送了 2 次，各等 3 秒"
    return [
        *placed(order_id),
        _make(contracts.PaymentAuthorized, 1, order_id, payment_id="P001"),
        _make(contracts.PaymentRefunded, 8, order_id, payment_id="P001", amount=230),
        _make(contracts.OrderCancelled, 9, order_id, reason=reason),
        _make(contracts.TicketAccepted, 20, order_id, ticket_id="T001"),
        _make(contracts.TicketAccepted, 20, order_id, ticket_id="T001", again=True),  # 重送的那一則也回了
        _make(contracts.TicketCancelled, 21, order_id, ticket_id="T001", reason=reason),
    ]


SCENARIOS = {"happy": happy, "declined": declined, "rejected": rejected, "refund_failed": refund_failed}
