"""付款的應用服務：要授權就向金流服務要授權、要退款就退款，公告結果。

第 7 集（編舞式）：聽到「付款已請求」PaymentRequested 就授權。
第 8 集（編排式）：收到編排器的命令 AuthorizePayment／RefundPayment 才動，做完回覆一則事件。
"""
from dataclasses import replace

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

    # ------------------------------------------------------------ 第 7 集：編舞式
    # 訂閱 PaymentRequested：同一則收到兩次，只授權一次
    def on_payment_requested(self, event: contracts.PaymentRequested) -> None:
        if self._payments.seen(event.event_id):
            print(f"付款：這則 PaymentRequested 處理過了（訂單 {event.order_id}），不再授權", flush=True)
            return
        payment, message = self._authorize(event.order_id, event.customer_id, event.amount)
        if not self._payments.add(payment, message_id=event.event_id, announce=[message]):
            print(f"付款：這則訊息剛剛已經處理過了（訂單 {event.order_id}）", flush=True)
            return
        self._say(payment, message)

    # ------------------------------------------------------------ 第 8 集：編排式，聽命令、回覆結果
    def on_authorize_payment(self, command: contracts.AuthorizePayment) -> None:
        """授權。同一張訂單已經有結果了（編排器逾時重送）：不再找金流服務，把上次的結果再回一次。"""
        existing = self._payments.by_order(command.order_id)
        if existing is not None:
            reply = self._reply_for(existing)
            if self._payments.save(None, message_id=command.event_id, announce=[reply]):
                print(f"付款：{command.order_id} 已經是 {existing.status.value}，再回一次 {type(reply).__name__}", flush=True)
            return
        payment, message = self._authorize(command.order_id, command.customer_id, command.amount)
        if self._payments.add(payment, message_id=command.event_id, announce=[message]):
            self._say(payment, message)

    def on_refund_payment(self, command: contracts.RefundPayment) -> None:
        """退款（補償）。退成功→PaymentRefunded；金流服務退不了→RefundFailed（編排器決定要不要再試）。"""
        payment = self._payments.by_order(command.order_id)
        if payment is None or payment.status is PaymentStatus.FAILED:  # 沒有扣到款：不用退
            payment_id = payment.payment_id if payment else ""
            reply = contracts.PaymentRefunded(order_id=command.order_id, payment_id=payment_id, amount=0)
            if self._payments.save(None, message_id=command.event_id, announce=[reply]):
                print(f"付款：{command.order_id} 沒有扣到款，不用退，回覆 PaymentRefunded（0 元）", flush=True)
            return
        if payment.status is PaymentStatus.REFUNDED:  # 退過了（重送）：再回一次
            reply = self._reply_for(payment)
            if self._payments.save(None, message_id=command.event_id, announce=[reply]):
                print(f"付款：{payment.payment_id} 已經退過了，再回一次 PaymentRefunded", flush=True)
            return
        result = self._gateway.refund(payment.order_id, payment.amount)  # 呼叫外面的金流服務
        if result.approved:
            changed = replace(payment, status=PaymentStatus.REFUNDED, reason="")
            reply = self._reply_for(changed)
            how = f"退 {payment.amount} 元（退款編號 {result.reference}）"
        else:
            changed = replace(payment, reason=f"退款失敗：{result.reason}")  # 還是 authorized：錢還沒退
            reply = contracts.RefundFailed(order_id=payment.order_id, payment_id=payment.payment_id, reason=result.reason)
            how = f"退款失敗（{result.reason}）"
        if self._payments.save(changed, message_id=command.event_id, announce=[reply]):
            print(f"付款：{payment.payment_id} {payment.order_id} {how}，回覆 {type(reply).__name__}", flush=True)

    # ------------------------------------------------------------ 共用
    def _authorize(self, order_id: str, customer_id: str, amount: int) -> tuple[Payment, contracts.IntegrationEvent]:
        result = self._gateway.authorize(order_id, amount)  # 呼叫外面的金流服務
        status = PaymentStatus.AUTHORIZED if result.approved else PaymentStatus.FAILED
        payment = Payment(
            self._payments.next_id(),
            order_id,
            customer_id,
            amount,
            status,
            reference=result.reference,
            reason=result.reason,
        )
        return payment, self._reply_for(payment)

    @staticmethod
    def _reply_for(payment: Payment) -> contracts.IntegrationEvent:
        """付款現在的樣子，用共同語言說一次。"""
        if payment.status is PaymentStatus.AUTHORIZED:
            return contracts.PaymentAuthorized(order_id=payment.order_id, payment_id=payment.payment_id)
        if payment.status is PaymentStatus.REFUNDED:
            return contracts.PaymentRefunded(order_id=payment.order_id, payment_id=payment.payment_id, amount=payment.amount)
        return contracts.PaymentFailed(order_id=payment.order_id, payment_id=payment.payment_id, reason=payment.reason)

    @staticmethod
    def _say(payment: Payment, message: contracts.IntegrationEvent) -> None:
        how = f"授權碼 {payment.reference}" if payment.status is PaymentStatus.AUTHORIZED else payment.reason
        print(
            f"付款：{payment.payment_id} {payment.order_id} {payment.amount} 元 → {payment.status.value}（{how}），"
            f"公告 {type(message).__name__}",
            flush=True,
        )
