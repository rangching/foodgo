"""第 7 集：編舞式 Saga 的每一棒——付款（防腐層＋假的金流服務）、廚房（付款過了才接單）、外送（接單了才派人）。

執行：uv run pytest tests/ep07 -q（這個檔不用 Docker：全部用記憶體版的 Repository）
"""
import pytest

from foodgo import contracts
from foodgo.delivery.consumer import handle as delivery_handle
from foodgo.delivery.delivery import Delivery, DeliveryRuleViolation, DeliveryStatus
from foodgo.delivery.repository import InMemoryDeliveryRepository
from foodgo.delivery.service import Dispatch
from foodgo.kitchen.consumer import handle as kitchen_handle
from foodgo.kitchen.repository import InMemoryTicketRepository
from foodgo.kitchen.service import Kitchen
from foodgo.kitchen.ticket import TicketRuleViolation, TicketStatus
from foodgo.ordering.integration import announcements
from foodgo.ordering.model import Address, Money, Order, OrderStatus
from foodgo.payment import gateway
from foodgo.payment.consumer import handle as payment_handle
from foodgo.payment.fakepay import FakePay
from foodgo.payment.gateway import Authorization, FakePayAdapter
from foodgo.payment.payment import PaymentStatus
from foodgo.payment.repository import InMemoryPaymentRepository
from foodgo.payment.service import Payments


def a001() -> Order:
    order = Order(
        order_id="A001",
        customer_id="C042",
        restaurant_id="R007",
        delivery_address=Address("信義路五段 7 號"),
        delivery_fee=Money(30),
        note="不要香菜",
    )
    order.add_line("牛肉麵", Money(160))
    order.add_line("紅茶", Money(40))
    order.place()
    return order


def placed() -> tuple[contracts.OrderPlaced, contracts.PaymentRequested, contracts.DeliveryRequested]:
    order = a001()
    order_placed, payment, delivery = announcements(order, order.pull_events())
    return order_placed, payment, delivery


def payments(fault: str = "") -> tuple[Payments, InMemoryPaymentRepository]:
    repo = InMemoryPaymentRepository()
    return Payments(FakePayAdapter(FakePay(fault=fault)), repo), repo


def kitchen(fault: str = "") -> tuple[Kitchen, InMemoryTicketRepository]:
    repo = InMemoryTicketRepository()
    return Kitchen(tickets=repo, fault=fault), repo


def dispatch() -> tuple[Dispatch, InMemoryDeliveryRepository]:
    repo = InMemoryDeliveryRepository()
    return Dispatch(repo), repo


AUTHORIZED = contracts.PaymentAuthorized(order_id="A001", payment_id="P001")
DECLINED = contracts.PaymentFailed(order_id="A001", payment_id="P001", reason="卡片額度不足")
ACCEPTED = contracts.TicketAccepted(order_id="A001", ticket_id="T001")


# ---------------------------------------------------------------- 防腐層：我們說「元」，金流服務說「分」
class Spy(FakePay):
    """記下防腐層送給金流服務的請求。"""

    def create_authorization(self, request):
        self.request = request
        return super().create_authorization(request)


def test_the_adapter_speaks_yuan_and_the_gateway_speaks_cents():
    client = Spy()
    result = FakePayAdapter(client).authorize("A001", 230)
    assert client.request == {"merchant_order_no": "A001", "amount_minor": 23000, "currency": "TWD"}  # 230 元＝23000 分
    assert result == Authorization(approved=True, reference="AUTH-0001")


def test_a_declined_card_comes_back_in_our_words():
    result = FakePayAdapter(FakePay(fault="decline")).authorize("A001", 230)
    assert result == Authorization(approved=False, reason="卡片額度不足")  # 不是 "insufficient_funds"


def test_the_same_order_is_authorized_only_once():
    adapter = FakePayAdapter(FakePay())
    assert adapter.authorize("A001", 230).reference == adapter.authorize("A001", 230).reference == "AUTH-0001"
    assert adapter.authorize("A002", 230).reference == "AUTH-0002"


def test_the_fault_switch_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("FOODGO_FAULT_PAYMENT", "decline")
    assert gateway.from_environment().authorize("A001", 230).approved is False
    monkeypatch.delenv("FOODGO_FAULT_PAYMENT")
    assert gateway.from_environment().authorize("A001", 230).approved is True


# ---------------------------------------------------------------- 付款：一棒
def test_payment_requested_is_authorized_and_announced():
    service, repo = payments()
    _, request, _ = placed()
    service.on_payment_requested(request)
    [payment] = repo.all()
    assert (payment.payment_id, payment.order_id, payment.amount, payment.status) == (
        "P001",
        "A001",
        230,
        PaymentStatus.AUTHORIZED,
    )
    assert [(type(m).__name__, m.order_id, m.payment_id) for m in repo.announced] == [
        ("PaymentAuthorized", "A001", "P001")
    ]


def test_a_declined_card_announces_payment_failed():
    service, repo = payments(fault="decline")
    _, request, _ = placed()
    service.on_payment_requested(request)
    assert repo.by_order("A001").status is PaymentStatus.FAILED
    [failed] = repo.announced
    assert (type(failed).__name__, failed.reason) == ("PaymentFailed", "卡片額度不足")


def test_the_same_request_twice_is_handled_once(capsys):
    service, repo = payments()
    _, request, _ = placed()
    service.on_payment_requested(request)
    service.on_payment_requested(contracts.PaymentRequested.from_json(request.to_json()))  # Kafka 又送來一次
    assert len(repo.all()) == 1 and len(repo.announced) == 1
    assert "處理過了" in capsys.readouterr().out


def test_the_payment_consumer_only_picks_payment_requested(kafka_message):
    service, repo = payments()
    for offset, message in enumerate(placed()):
        payment_handle(service, kafka_message(message, offset))
    assert [p.order_id for p in repo.all()] == ["A001"]


# ---------------------------------------------------------------- 廚房：付款過了才接單
def test_the_kitchen_opens_a_ticket_and_waits_for_the_payment(capsys):
    service, repo = kitchen()
    order_placed, *_ = placed()
    service.on_order_placed(order_placed)
    [ticket] = repo.all()
    assert (ticket.ticket_id, ticket.status) == ("T001", TicketStatus.RECEIVED)
    assert repo.announced == []
    assert "T001 等付款" in capsys.readouterr().out


def test_payment_authorized_then_the_kitchen_accepts():
    service, repo = kitchen()
    order_placed, *_ = placed()
    service.on_order_placed(order_placed)
    service.on_payment_authorized(AUTHORIZED)
    assert repo.by_order("A001").status is TicketStatus.ACCEPTED
    assert [(type(m).__name__, m.order_id, m.ticket_id) for m in repo.announced] == [("TicketAccepted", "A001", "T001")]


def test_the_payment_can_arrive_before_the_order():
    """不同 topic 之間沒有先後保證：PaymentAuthorized 先到，廚房先記著，OrderPlaced 一到就接單。"""
    service, repo = kitchen()
    service.on_payment_authorized(AUTHORIZED)
    assert repo.all() == [] and repo.payment_of("A001") == ("authorized", "")
    order_placed, *_ = placed()
    service.on_order_placed(order_placed)
    assert repo.by_order("A001").status is TicketStatus.ACCEPTED
    assert len(repo.announced) == 1


def test_a_failed_payment_means_the_ticket_is_not_cooked():
    service, repo = kitchen()
    order_placed, *_ = placed()
    service.on_order_placed(order_placed)
    service.on_payment_failed(DECLINED)
    assert repo.by_order("A001").status is TicketStatus.CANCELLED
    assert repo.announced == []  # 廚房只管自己：沒有公告任何事，外送不會知道


def test_the_same_payment_twice_accepts_once():
    service, repo = kitchen()
    order_placed, *_ = placed()
    service.on_order_placed(order_placed)
    service.on_payment_authorized(AUTHORIZED)
    service.on_payment_authorized(contracts.PaymentAuthorized.from_json(AUTHORIZED.to_json()))
    assert len(repo.announced) == 1


def test_the_reject_fault_rejects_after_payment():
    service, repo = kitchen(fault="reject")
    order_placed, *_ = placed()
    service.on_order_placed(order_placed)
    service.on_payment_authorized(AUTHORIZED)
    assert repo.by_order("A001").status is TicketStatus.REJECTED
    [rejected] = repo.announced
    assert (type(rejected).__name__, rejected.reason) == ("TicketRejected", "餐廳太忙，暫停接單")


def test_ticket_rules():
    service, repo = kitchen()
    order_placed, *_ = placed()
    service.on_order_placed(order_placed)
    service.on_payment_failed(DECLINED)
    ticket = repo.by_order("A001")
    with pytest.raises(TicketRuleViolation):
        ticket.mark_ready()  # 不做了的單不能出餐
    with pytest.raises(TicketRuleViolation):
        ticket.accept()  # 已經決定過了


def test_the_kitchen_consumer_routes_payment_events(kafka_message):
    service, repo = kitchen()
    order_placed, *_ = placed()
    kitchen_handle(service, kafka_message(order_placed))
    kitchen_handle(service, kafka_message(AUTHORIZED))
    assert repo.by_order("A001").status is TicketStatus.ACCEPTED


# ---------------------------------------------------------------- 外送：餐廳接單了才派人
def test_delivery_opens_a_delivery_and_waits_for_the_kitchen(capsys):
    service, repo = dispatch()
    *_, request = placed()
    service.on_delivery_requested(request)
    [delivery] = repo.all()
    assert (delivery.delivery_id, delivery.pickup, delivery.dropoff, delivery.status) == (
        "D001",
        "R007",
        "信義路五段 7 號",
        DeliveryStatus.WAITING,
    )
    assert "D001 等餐廳接單" in capsys.readouterr().out


def test_ticket_accepted_then_a_courier_is_assigned():
    service, repo = dispatch()
    *_, request = placed()
    service.on_delivery_requested(request)
    service.on_ticket_accepted(ACCEPTED)
    assert repo.by_order("A001").courier_id == "M01"
    assert [(type(m).__name__, m.delivery_id, m.courier_id) for m in repo.announced] == [
        ("CourierAssigned", "D001", "M01")
    ]


def test_the_kitchen_can_answer_before_the_delivery_request():
    service, repo = dispatch()
    service.on_ticket_accepted(ACCEPTED)
    *_, request = placed()
    service.on_delivery_requested(request)
    assert repo.by_order("A001").status is DeliveryStatus.ASSIGNED
    assert len(repo.announced) == 1


def test_couriers_take_turns():
    service, repo = dispatch()
    for order_id in ["A001", "A002"]:
        service.on_delivery_requested(
            contracts.DeliveryRequested(order_id=order_id, restaurant_id="R007", delivery_address="信義路五段 7 號")
        )
        service.on_ticket_accepted(contracts.TicketAccepted(order_id=order_id, ticket_id="T001"))
    assert [d.courier_id for d in repo.all()] == ["M01", "M02"]


def test_a_delivery_is_assigned_once():
    delivery = Delivery("D001", "A001", "R007", "信義路五段 7 號")
    delivery.assign("M01")
    with pytest.raises(DeliveryRuleViolation):
        delivery.assign("M02")


def test_the_delivery_consumer_routes_kitchen_events(kafka_message):
    service, repo = dispatch()
    for offset, message in enumerate(placed()):
        delivery_handle(service, kafka_message(message, offset))
    delivery_handle(service, kafka_message(ACCEPTED))
    assert repo.by_order("A001").status is DeliveryStatus.ASSIGNED


# ---------------------------------------------------------------- 整條接力（記憶體版）
def relay_race(fault_payment: str = ""):
    """手動當 Kafka：每一棒公告的事件，交給聽它的人。回傳每個上下文的 Repository 和經過的事件。"""
    order = a001()
    pay, pay_repo = payments(fault_payment)
    cook, cook_repo = kitchen()
    send, send_repo = dispatch()
    order_placed, request_payment, request_delivery = announcements(order, order.pull_events())
    cook.on_order_placed(order_placed)
    send.on_delivery_requested(request_delivery)
    pay.on_payment_requested(request_payment)
    for message in pay_repo.announced:
        if isinstance(message, contracts.PaymentAuthorized):
            cook.on_payment_authorized(message)
        else:
            cook.on_payment_failed(message)
    for message in cook_repo.announced:
        send.on_ticket_accepted(message)
    seen = [
        order_placed,
        request_payment,
        request_delivery,
        *pay_repo.announced,
        *cook_repo.announced,
        *send_repo.announced,
    ]
    return order, pay_repo, cook_repo, send_repo, [type(m).__name__ for m in seen]


def test_the_whole_relay_ends_with_a_courier():
    _, pay, cook, send, seen = relay_race()
    assert seen == [
        "OrderPlaced",
        "PaymentRequested",
        "DeliveryRequested",
        "PaymentAuthorized",
        "TicketAccepted",
        "CourierAssigned",
    ]
    assert (pay.by_order("A001").status, cook.by_order("A001").status, send.by_order("A001").status) == (
        PaymentStatus.AUTHORIZED,
        TicketStatus.ACCEPTED,
        DeliveryStatus.ASSIGNED,
    )


def test_after_a_declined_payment_nobody_tells_ordering():
    """編舞式的難處：付款失敗，廚房自己不做了；可是點餐、外送都沒聽到，沒有人負責把訂單取消。"""
    order, pay, cook, send, seen = relay_race(fault_payment="decline")
    assert seen == ["OrderPlaced", "PaymentRequested", "DeliveryRequested", "PaymentFailed"]
    assert pay.by_order("A001").status is PaymentStatus.FAILED
    assert cook.by_order("A001").status is TicketStatus.CANCELLED
    assert send.by_order("A001").status is DeliveryStatus.WAITING  # 還在等一張不會來的 TicketAccepted
    assert order.status is OrderStatus.PLACED  # 點餐沒有聽 payment.events：訂單還是「已送出」
