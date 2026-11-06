"""第 7 集：付款、廚房、外送的共同語言——新的整合事件與 topic，「付款要金額、外送要地址」放在哪，以及邊界。

執行：uv run pytest tests/ep07 -q（這個檔不用 Docker）
"""
import dataclasses
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from foodgo import contracts
from foodgo.infra import outbox
from foodgo.ordering.integration import announcements
from foodgo.ordering.model import Address, Money, Order

ROOT = Path(__file__).resolve().parents[2]


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


def placed() -> list[contracts.IntegrationEvent]:
    order = a001()
    return announcements(order, order.pull_events())


def data_of(message: contracts.IntegrationEvent) -> dict:
    return json.loads(message.to_json())["data"]


# ---------------------------------------------------------------- topic：一個上下文一個
def test_each_context_announces_on_its_own_topic():
    assert (contracts.ORDERING_TOPIC, contracts.PAYMENT_TOPIC, contracts.KITCHEN_TOPIC, contracts.DELIVERY_TOPIC) == (
        "ordering.events",
        "payment.events",
        "kitchen.events",
        "delivery.events",
    )


def test_consumer_groups_are_named_after_the_contexts():
    from foodgo.delivery import consumer as delivery
    from foodgo.kitchen import consumer as kitchen
    from foodgo.payment import consumer as payment

    assert (payment.GROUP, kitchen.GROUP, delivery.GROUP) == ("payment", "kitchen", "delivery")
    assert payment.LISTENS == ("ordering.events",)
    assert kitchen.LISTENS == ("ordering.events", "payment.events")
    assert delivery.LISTENS == ("ordering.events", "kitchen.events")


# ---------------------------------------------------------------- 送出一張訂單：三則，各給一個上下文
def test_placing_an_order_announces_three_messages():
    messages = placed()
    assert [type(m).__name__ for m in messages] == ["OrderPlaced", "PaymentRequested", "DeliveryRequested"]
    assert {m.order_id for m in messages} == {"A001"}


def test_the_kitchen_still_gets_no_prices_and_no_address():
    order_placed, *_ = placed()
    assert data_of(order_placed) == {
        "order_id": "A001",
        "restaurant_id": "R007",
        "items": [{"name": "牛肉麵", "quantity": 1}, {"name": "紅茶", "quantity": 1}],
        "note": "不要香菜",
    }  # 和第 3 集一模一樣：OrderPlaced 沒有改


def test_payment_gets_the_amount_but_not_the_dishes():
    _, payment, _ = placed()
    assert data_of(payment) == {"order_id": "A001", "customer_id": "C042", "amount": 230}


def test_delivery_gets_the_address_but_not_the_dishes_or_prices():
    *_, delivery = placed()
    assert data_of(delivery) == {"order_id": "A001", "restaurant_id": "R007", "delivery_address": "信義路五段 7 號"}


def test_one_fact_one_set_of_ids():
    order = a001()
    events = order.pull_events()
    first, again = announcements(order, events), announcements(order, events)
    assert [m.event_id for m in first] == [m.event_id for m in again]  # 翻幾次都是同一組編號：outbox 不會多一則
    assert first[0].event_id == str(events[-1].event_id)  # OrderPlaced 沿用領域事件的編號（第 3 集）
    assert len({m.event_id for m in first}) == 3
    assert {m.occurred_at for m in first} == {first[0].occurred_at}


def test_drafts_and_cancellations_announce_nothing():
    order = a001()
    order.pull_events()
    order.cancel("顧客改變心意")
    assert announcements(order, order.pull_events()) == []  # OrderCancelled 還是不公告（第 8 集補）


def test_the_new_events_are_version_1_and_round_trip():
    samples = [
        *placed(),
        contracts.PaymentAuthorized(order_id="A001", payment_id="P001"),
        contracts.PaymentFailed(order_id="A002", payment_id="P002", reason="卡片額度不足"),
        contracts.TicketAccepted(order_id="A001", ticket_id="T001"),
        contracts.TicketRejected(order_id="A001", ticket_id="T001", reason="餐廳太忙，暫停接單"),
        contracts.CourierAssigned(order_id="A001", delivery_id="D001", courier_id="M01"),
    ]
    for event in samples:
        assert json.loads(event.to_json())["schema_version"] == 1
        assert type(event).from_json(event.to_json()) == event


def test_payment_authorized_carries_no_amount():
    fields = {f.name for f in dataclasses.fields(contracts.PaymentAuthorized)}
    assert fields == {"event_id", "occurred_at", "order_id", "payment_id"}  # 廚房不看價格；退款時付款自己查


def test_the_kitchen_skips_what_is_not_for_it(kafka_message):
    """廚房 consumer 只挑自己要的：同一個 topic 多了給付款、外送的事件，照樣讀得下去，略過就好。"""
    from foodgo.kitchen.consumer import handle
    from foodgo.kitchen.service import Kitchen

    kitchen = Kitchen()
    for offset, message in enumerate(placed()[1:], start=1):
        handle(kitchen, kafka_message(message, offset))
    assert kitchen.tickets() == []


# ---------------------------------------------------------------- 各自的表、各自的 outbox
def test_each_context_has_its_own_tables():
    from foodgo.delivery.postgres import SCHEMAS as DELIVERY
    from foodgo.kitchen.postgres import SCHEMAS as KITCHEN
    from foodgo.payment.postgres import SCHEMAS as PAYMENT

    for schemas, tables in [
        (PAYMENT, ["payments", "payment_outbox", "processed_messages"]),
        (KITCHEN, ["tickets", "kitchen_payments", "kitchen_outbox", "processed_messages"]),
        (DELIVERY, ["deliveries", "delivery_tickets", "delivery_outbox", "processed_messages"]),
    ]:
        text = "\n".join(schemas)
        for table in tables:
            assert f"CREATE TABLE IF NOT EXISTS {table} " in text, table


def test_an_outbox_table_can_have_any_name():
    assert outbox.schema() == outbox.SCHEMA  # 點餐的 outbox 沒有變
    assert "CREATE TABLE IF NOT EXISTS kitchen_outbox (" in outbox.schema("kitchen_outbox")


# ---------------------------------------------------------------- 邊界
def lint_imports(cwd, env=None):
    return subprocess.run(["lint-imports"], cwd=cwd, env=env, capture_output=True, text=True)


def test_boundary_rules_hold():
    result = lint_imports(ROOT)
    assert result.returncode == 0, result.stdout
    assert "四個上下文彼此獨立 KEPT" in result.stdout
    assert "付款只透過防腐層找金流服務 KEPT" in result.stdout


@pytest.mark.parametrize(
    "where, line, message",
    [
        (
            "payment/service.py",
            "from foodgo.payment.fakepay import FakePay\n",
            "foodgo.payment.service is not allowed to import foodgo.payment.fakepay",
        ),
        (
            "kitchen/service.py",
            "from foodgo.payment.payment import PaymentStatus\n",
            "foodgo.kitchen is not allowed to import foodgo.payment",
        ),
        (
            "delivery/service.py",
            "from foodgo.kitchen.ticket import Ticket\n",
            "foodgo.delivery is not allowed to import foodgo.kitchen",
        ),
    ],
    ids=["payment-skips-the-acl", "kitchen-reads-payment", "delivery-reads-kitchen"],
)
def test_back_doors_are_caught(tmp_path, where, line, message):
    shutil.copytree(ROOT / "src", tmp_path / "src")
    shutil.copy(ROOT / ".importlinter", tmp_path)
    target = tmp_path / "src" / "foodgo" / where
    target.write_text(target.read_text(encoding="utf-8") + "\n" + line, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "src")}
    result = lint_imports(tmp_path, env)
    assert result.returncode == 1
    assert message in result.stdout


# ---------------------------------------------------------------- 詞彙表
def test_every_contract_is_in_the_glossary():
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    names = [
        n
        for n in contracts.__all__
        if isinstance(getattr(contracts, n), type) and n not in {"IntegrationEvent", "OrderItem"}
    ]
    for name in names:
        assert f"`{name}`" in glossary, f"整合事件 {name} 要寫進 glossary.md"


@pytest.mark.parametrize(
    "term",
    [
        "2PC",
        "Saga",
        "本地交易",
        "補償",
        "編舞式",
        "編排式",
        "防腐層",
        "payment.events",
        "kitchen.events",
        "delivery.events",
        "kitchen_payments",
        "delivery_tickets",
        "FOODGO_FAULT_PAYMENT",
        "誰先到",
    ],
)
def test_glossary_explains_the_choreography(term):
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    assert term in glossary
