"""付款的應用服務：聽到「付款已請求」就向金流服務要授權，公告授權成功或失敗。"""
from foodgo import contracts
from foodgo.payment.gateway import CardGateway
from foodgo.payment.payment import Payment, PaymentStatus
from foodgo.payment.repository import InMemoryPaymentRepository, PaymentRepository


class Payments:
    def __init__(self, gateway: CardGateway, payments: PaymentRepository | None = None) -> None:
        self._gateway = gateway  # 只認防腐層的介面，不知道後面是哪一家金流服務
        self._payments = payments or InMemoryPaymentRepository()

    def all(self) -> list[Payment]:
        return self._payments.all()

    def by_order(self, order_id: str) -> Payment | None:
        return self._payments.by_order(order_id)

    # 訂閱 PaymentRequested：同一則收到兩次，只授權一次
    def on_payment_requested(self, event: contracts.PaymentRequested) -> None:
        if self._payments.seen(event.event_id):
            print(f"付款：這則 PaymentRequested 處理過了（訂單 {event.order_id}），不再授權", flush=True)
            return
        result = self._gateway.authorize(event.order_id, event.amount)  # 呼叫外面的金流服務
        status = PaymentStatus.AUTHORIZED if result.approved else PaymentStatus.FAILED
        payment = Payment(
            self._payments.next_id(),
            event.order_id,
            event.customer_id,
            event.amount,
            status,
            reference=result.reference,
            reason=result.reason,
        )
        if result.approved:
            message = contracts.PaymentAuthorized(order_id=payment.order_id, payment_id=payment.payment_id)
        else:
            message = contracts.PaymentFailed(
                order_id=payment.order_id, payment_id=payment.payment_id, reason=payment.reason
            )
        if not self._payments.add(payment, message_id=event.event_id, announce=[message]):
            print(f"付款：這則訊息剛剛已經處理過了（訂單 {event.order_id}）", flush=True)
            return
        how = f"授權碼 {payment.reference}" if result.approved else payment.reason
        print(
            f"付款：{payment.payment_id} {event.order_id} {event.amount} 元 → {status.value}（{how}），"
            f"公告 {type(message).__name__}",
            flush=True,
        )
