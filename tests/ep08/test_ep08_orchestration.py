"""第 8 集：編排器指揮四個參與者——一路走完、付款失敗、拒單退款、廚房沒回應、廚房很慢、退款失敗。

執行：uv run pytest tests/ep08 -q（這個檔不用 Docker：參與者用記憶體版的 Repository，中間用 list 當 Kafka，時間用假的時鐘）
"""
from ep08_world import Orders, World, a001, facts

from foodgo import contracts
from foodgo.delivery.consumer import handle as delivery_handle
from foodgo.delivery.delivery import DeliveryStatus
from foodgo.kitchen.consumer import handle as kitchen_handle
from foodgo.kitchen.ticket import TicketStatus
from foodgo.ordering.consumer import handle as ordering_handle
from foodgo.ordering.consumer import on_cancel_order
from foodgo.ordering.model import OrderStatus
from foodgo.payment.consumer import handle as payment_handle
from foodgo.payment.payment import PaymentStatus
from foodgo.saga.consumer import handle as saga_handle
from foodgo.saga.machine import SagaState
from foodgo.saga.orchestrator import Orchestrator


def statuses(world: World) -> tuple:
    payment = world.payments.by_order("A001")
    ticket = world.tickets.by_order("A001")
    delivery = world.deliveries.by_order("A001")
    return (
        world.orders.get("A001").status.value,
        payment.status.value if payment else None,
        ticket.status.value if ticket else None,
        delivery.status.value if delivery else None,
        world.saga.state.value,
    )


def replies(world: World) -> list[str]:
    every = world.payments.announced + world.tickets.announced + world.deliveries.announced + world.orders.announced
    return [type(m).__name__ for m in every]


# ---------------------------------------------------------------- 一路走完
def test_a001_goes_all_the_way_under_the_orchestrator():
    world = World()
    world.place()
    assert statuses(world) == ("placed", "authorized", "accepted", "assigned", "completed")
    assert world.handled == ["AuthorizePayment", "AcceptTicket", "AssignCourier"]
    assert world.deliveries.by_order("A001").courier_id == "M01"
    assert [(state, action, message) for state, action, message in world.log() if action != "receive"] == [
        ("payment_pending", "send", "AuthorizePayment"),
        ("kitchen_pending", "send", "AcceptTicket"),
        ("delivery_pending", "send", "AssignCourier"),
        ("completed", "finish", ""),
    ]


# ---------------------------------------------------------------- 付款失敗：直接取消
def test_a_declined_payment_cancels_the_order():
    world = World(payment_fault="decline")
    world.place()
    assert statuses(world) == ("cancelled", "failed", None, None, "cancelled")  # 廚房、外送根本沒被叫到
    assert world.handled == ["AuthorizePayment", "CancelOrder"]
    cancelled = world.orders.announced[-1]
    assert (type(cancelled).__name__, cancelled.reason) == ("OrderCancelled", "付款失敗：卡片額度不足")


# ---------------------------------------------------------------- 付款成功、餐廳拒單：退款、取消
def test_a_rejected_ticket_is_refunded_and_the_order_cancelled():
    world = World(kitchen_fault="reject")
    world.place()
    assert statuses(world) == ("cancelled", "refunded", "rejected", None, "cancelled")
    assert world.handled == ["AuthorizePayment", "AcceptTicket", "RefundPayment", "CancelOrder"]
    assert [type(m).__name__ for m in world.payments.announced] == ["PaymentAuthorized", "PaymentRefunded"]
    refunded = next(m for m in world.payments.announced if isinstance(m, contracts.PaymentRefunded))
    assert (refunded.payment_id, refunded.amount) == ("P001", 230)
    assert world.orders.announced[-1].reason == "餐廳拒單：餐廳太忙，暫停接單"
    assert [(state, action, message) for state, action, message in world.log()][-6:] == [
        ("kitchen_pending", "receive", "TicketRejected"),
        ("compensating", "compensate", "RefundPayment"),
        ("compensating", "receive", "PaymentRefunded"),
        ("compensating", "compensate", "CancelOrder"),
        ("compensating", "receive", "OrderCancelled"),
        ("cancelled", "finish", ""),
    ]


# ---------------------------------------------------------------- 廚房沒回應：重試一次、補償；修好後單子也取消
def test_a_silent_kitchen_is_retried_then_compensated():
    world = World(down=("kitchen",))
    world.place()
    assert world.saga.state is SagaState.KITCHEN_PENDING
    world.wait(3)
    assert [a for _, a, m in world.log()][-2:] == ["timeout", "retry"]
    world.wait(3)
    assert statuses(world) == ("cancelled", "refunded", None, None, "cancelled")
    assert world.saga.reason == "廚房沒有回覆：送了 2 次，各等 3 秒"

    world.revive("kitchen")  # 修好重啟：排著的 AcceptTicket ×2、CancelTicket 照順序處理
    assert world.handled[-3:] == ["AcceptTicket", "AcceptTicket", "CancelTicket"]
    assert world.tickets.by_order("A001").status is TicketStatus.CANCELLED  # 先接單、再撤銷
    assert world.saga.state is SagaState.CANCELLED  # 遲到的回覆只記一筆 ignore
    assert [a for _, a, m in world.log()][-3:] == ["ignore", "ignore", "ignore"]


# ---------------------------------------------------------------- 廚房很慢：重試期間回覆到了，照樣走完
def test_a_slow_kitchen_answers_during_the_retry():
    world = World(slow={"kitchen": 4})
    world.place()
    world.wait(8)
    assert statuses(world) == ("placed", "authorized", "accepted", "assigned", "completed")
    assert world.handled.count("AcceptTicket") == 2  # 重送了一次
    assert len(world.tickets.all()) == 1  # 只有一張廚房單
    actions = [(a, m) for _, a, m in world.log()]
    assert ("timeout", "AcceptTicket") in actions and ("retry", "AcceptTicket") in actions
    assert actions[-1] == ("ignore", "TicketAccepted")  # 重送那一則的回覆：重複的，略過


# ---------------------------------------------------------------- 補償本身失敗：重試、人工處理
def test_a_refund_that_keeps_failing_needs_attention():
    world = World(kitchen_fault="reject", refund_fault="fail")
    world.place()
    assert world.saga.state is SagaState.NEEDS_ATTENTION
    assert world.handled == ["AuthorizePayment", "AcceptTicket", "RefundPayment", "RefundPayment"]
    payment = world.payments.by_order("A001")
    assert (payment.status, payment.reason) == (PaymentStatus.AUTHORIZED, "退款失敗：發卡銀行暫時無法退款")
    assert world.orders.get("A001").status is OrderStatus.PLACED  # 錢還沒退，訂單不會默默被取消
    assert [(a, m) for _, a, m in world.log()][-5:] == [
        ("compensate", "RefundPayment"),
        ("receive", "RefundFailed"),
        ("retry", "RefundPayment"),
        ("receive", "RefundFailed"),
        ("give_up", ""),
    ]


# ---------------------------------------------------------------- 參與者：重送的命令只做一次、再回一次
def test_participants_answer_retried_commands_again():
    world = World()
    world.place()
    again = contracts.AuthorizePayment(order_id="A001", customer_id="C042", amount=230)  # 新的編號：編排器的重送
    world.payment.on_authorize_payment(again)
    world.kitchen.on_accept_ticket(
        contracts.AcceptTicket(order_id="A001", restaurant_id="R007", items=(contracts.OrderItem(name="牛肉麵", quantity=1),))
    )
    world.dispatch.on_assign_courier(
        contracts.AssignCourier(order_id="A001", restaurant_id="R007", delivery_address="信義路五段 7 號")
    )
    assert (len(world.payments.all()), len(world.tickets.all()), len(world.deliveries.all())) == (1, 1, 1)
    assert replies(world).count("PaymentAuthorized") == 2
    assert replies(world).count("TicketAccepted") == 2
    assert replies(world).count("CourierAssigned") == 2
    world.pump()
    assert world.saga.state is SagaState.COMPLETED  # 重複的回覆不會讓 saga 亂跳


def test_the_same_command_delivered_twice_is_done_once():
    world = World()
    world.place()
    [(_, authorize)] = [(t, c) for t, c in world.sagas.sent if isinstance(c, contracts.AuthorizePayment)]
    before = len(world.payments.announced)
    world.payment.on_refund_payment(contracts.RefundPayment(order_id="A001", reason="測試"))
    world.payment.on_authorize_payment(authorize)  # Kafka 又送來同一則（同一個 event_id）
    assert len(world.payments.announced) == before + 1  # 只有退款那一則
    assert world.payments.by_order("A001").status is PaymentStatus.REFUNDED


def test_a_ticket_already_cooked_cannot_be_cancelled(capsys):
    world = World()
    world.place()
    ticket = world.tickets.by_order("A001")
    world.kitchen.mark_ready(ticket)
    world.kitchen.on_cancel_ticket(contracts.CancelTicket(order_id="A001", reason="測試"))
    assert world.tickets.by_order("A001").status is TicketStatus.READY  # 做好了就回不去
    assert "已經出餐了，取消不了" in capsys.readouterr().out


def test_cancelling_an_order_twice_is_harmless():
    world = World(kitchen_fault="reject")
    world.place()
    on_cancel_order(world.orders, contracts.CancelOrder(order_id="A001", reason="再一次"))
    assert [type(m).__name__ for m in world.orders.announced].count("OrderCancelled") == 1


# ---------------------------------------------------------------- consumer 認得自己的命令
def test_each_consumer_picks_its_own_commands(kafka_message):
    world = World()
    orchestrator = world.orchestrator
    for offset, message in enumerate(facts()):
        saga_handle(orchestrator, kafka_message(message, offset))
    [(_, authorize)] = world.sagas.sent
    payment_handle(world.payment, kafka_message(authorize))
    assert world.payments.by_order("A001").status is PaymentStatus.AUTHORIZED

    kitchen_handle(world.kitchen, kafka_message(_accept()))
    assert world.tickets.by_order("A001").status is TicketStatus.ACCEPTED
    delivery_handle(world.dispatch, kafka_message(_assign()))
    assert world.deliveries.by_order("A001").status is DeliveryStatus.ASSIGNED

    orders = Orders()
    orders.save(a001())
    ordering_handle(orders, kafka_message(contracts.CancelOrder(order_id="A001", reason="測試")))
    assert orders.get("A001").status is OrderStatus.CANCELLED
    saga_handle(orchestrator, kafka_message(contracts.MealReady(order_id="A001", ticket_id="T001", restaurant_id="R007")))


def test_the_orchestrator_skips_what_it_does_not_hear():
    orchestrator = Orchestrator()
    assert orchestrator.on_payload(contracts.MealReady(order_id="A001", ticket_id="T001", restaurant_id="R007").to_json()) is None
    assert orchestrator.on_event(contracts.PaymentAuthorized(order_id="A999", payment_id="P001")) is None  # 沒有這個 saga
    assert orchestrator.get("A999") is None


def _accept() -> contracts.AcceptTicket:
    return contracts.AcceptTicket(
        order_id="A001", restaurant_id="R007", items=(contracts.OrderItem(name="牛肉麵", quantity=1),), note="不要香菜"
    )


def _assign() -> contracts.AssignCourier:
    return contracts.AssignCourier(order_id="A001", restaurant_id="R007", delivery_address="信義路五段 7 號")
