"""第 8 集：命令進了共同語言——命令 topic、祈使句的命令、新的事件（OrderCancelled 對外公告了）、誰聽什麼、邊界與詞彙表。

執行：uv run pytest tests/ep08 -q（這個檔不用 Docker）
"""
import dataclasses
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from ep08_world import a001

from foodgo import contracts
from foodgo.ordering.integration import announcements

ROOT = Path(__file__).resolve().parents[2]

COMMANDS = ["AuthorizePayment", "RefundPayment", "AcceptTicket", "CancelTicket", "AssignCourier", "CancelOrder"]
NEW_EVENTS = ["OrderCancelled", "PaymentRefunded", "RefundFailed", "TicketCancelled"]


# ---------------------------------------------------------------- 命令：祈使句、指名接收者、送到接收者的命令 topic
def test_command_topics_are_named_after_the_receiver():
    assert contracts.COMMAND_TOPICS == {
        "ordering": "ordering.commands",
        "payment": "payment.commands",
        "kitchen": "kitchen.commands",
        "delivery": "delivery.commands",
    }
    assert (contracts.ORDERING_TOPIC, contracts.PAYMENT_TOPIC) == ("ordering.events", "payment.events")  # 事件 topic 不變


def test_commands_name_their_receiver():
    receivers = {name: getattr(contracts, name).receiver for name in COMMANDS}
    assert receivers == {
        "AuthorizePayment": "payment",
        "RefundPayment": "payment",
        "AcceptTicket": "kitchen",
        "CancelTicket": "kitchen",
        "AssignCourier": "delivery",
        "CancelOrder": "ordering",
    }


def test_commands_and_events_are_different_kinds_of_message():
    for name in COMMANDS:
        assert issubclass(getattr(contracts, name), contracts.Command), name
        assert not issubclass(getattr(contracts, name), contracts.IntegrationEvent), name
    for name in NEW_EVENTS:
        assert issubclass(getattr(contracts, name), contracts.IntegrationEvent), name


def test_a_command_uses_the_same_envelope():
    command = contracts.AuthorizePayment(order_id="A001", customer_id="C042", amount=230)
    envelope = json.loads(command.to_json())
    assert set(envelope) == {"type", "schema_version", "event_id", "occurred_at", "data"}  # outbox、relay 不用改
    assert envelope["data"] == {"order_id": "A001", "customer_id": "C042", "amount": 230}
    assert "receiver" not in envelope["data"]  # 接收者看 topic 就知道，不放進資料


def test_the_new_messages_are_version_1_and_round_trip():
    samples = [
        contracts.AuthorizePayment(order_id="A001", customer_id="C042", amount=230),
        contracts.RefundPayment(order_id="A001", reason="餐廳拒單：餐廳太忙，暫停接單"),
        contracts.AcceptTicket(
            order_id="A001",
            restaurant_id="R007",
            items=(contracts.OrderItem(name="牛肉麵", quantity=1), contracts.OrderItem(name="紅茶", quantity=1)),
            note="不要香菜",
        ),
        contracts.CancelTicket(order_id="A001", reason="廚房沒有回覆"),
        contracts.AssignCourier(order_id="A001", restaurant_id="R007", delivery_address="信義路五段 7 號"),
        contracts.CancelOrder(order_id="A001", reason="付款失敗：卡片額度不足"),
        contracts.OrderCancelled(order_id="A001", reason="付款失敗：卡片額度不足"),
        contracts.PaymentRefunded(order_id="A001", payment_id="P001", amount=230),
        contracts.RefundFailed(order_id="A001", payment_id="P001", reason="發卡銀行暫時無法退款"),
        contracts.TicketCancelled(order_id="A001", ticket_id="T001", reason="廚房沒有回覆"),
    ]
    for message in samples:
        assert json.loads(message.to_json())["schema_version"] == 1
        assert type(message).from_json(message.to_json()) == message


def test_what_each_command_carries():
    def fields(name):
        return {f.name for f in dataclasses.fields(getattr(contracts, name))} - {"event_id", "occurred_at"}

    assert fields("AuthorizePayment") == {"order_id", "customer_id", "amount"}
    assert fields("RefundPayment") == {"order_id", "reason"}  # 不帶金額：付款自己知道授權了多少
    assert fields("AcceptTicket") == {"order_id", "restaurant_id", "items", "note"}  # 廚房還是不看價格
    assert fields("AssignCourier") == {"order_id", "restaurant_id", "delivery_address"}
    assert fields("CancelOrder") == {"order_id", "reason"}


# ---------------------------------------------------------------- OrderCancelled 對外公告了
def test_cancelling_an_order_announces_order_cancelled():
    order = a001()
    order.pull_events()
    order.cancel("顧客改變心意")
    [event] = order.pull_events()
    [message] = announcements(order, [event])
    assert isinstance(message, contracts.OrderCancelled)
    assert json.loads(message.to_json())["data"] == {"order_id": "A001", "reason": "顧客改變心意"}
    assert message.event_id == str(event.event_id)  # 同一件事，同一個編號（第 3 集的規則）


def test_placing_an_order_still_announces_the_same_three_facts():
    order = a001()
    assert [type(m).__name__ for m in announcements(order, order.pull_events())] == [
        "OrderPlaced",
        "PaymentRequested",
        "DeliveryRequested",
    ]


# ---------------------------------------------------------------- 誰聽什麼
def test_participants_listen_to_their_own_commands():
    from foodgo.delivery import consumer as delivery
    from foodgo.kitchen import consumer as kitchen
    from foodgo.ordering import consumer as ordering
    from foodgo.payment import consumer as payment

    assert (ordering.GROUP, ordering.LISTENS) == ("ordering", ("ordering.commands",))
    assert payment.LISTENS == ("payment.commands",)
    assert kitchen.LISTENS == ("kitchen.commands",)
    assert delivery.LISTENS == ("delivery.commands",)
    # 第 7 集的接力還在：FOODGO_SAGA=choreography
    assert payment.CHOREOGRAPHY == ("ordering.events",)
    assert kitchen.CHOREOGRAPHY == ("ordering.events", "payment.events")
    assert delivery.CHOREOGRAPHY == ("ordering.events", "kitchen.events")


def test_the_orchestrator_listens_to_every_events_topic():
    from foodgo.saga import consumer as saga

    assert saga.GROUP == "saga"
    assert saga.LISTENS == ("ordering.events", "payment.events", "kitchen.events", "delivery.events")


def test_the_saga_switch(monkeypatch):
    from foodgo.infra import config

    monkeypatch.delenv("FOODGO_SAGA", raising=False)
    assert config.saga_mode() == "orchestration"  # 預設：編排式
    monkeypatch.setenv("FOODGO_SAGA", "choreography")
    assert config.saga_mode() == "choreography"
    monkeypatch.setenv("FOODGO_SAGA", "orchestra")
    with pytest.raises(ValueError):
        config.saga_mode()


def test_the_saga_tables():
    from foodgo.saga.postgres import SCHEMAS

    text = "\n".join(SCHEMAS)
    for table in ["sagas", "saga_log", "saga_outbox", "processed_messages"]:
        assert f"CREATE TABLE IF NOT EXISTS {table} " in text, table


# ---------------------------------------------------------------- 邊界：編排器只說共同語言；參與者不知道誰在指揮
def lint_imports(cwd, env=None):
    return subprocess.run(["lint-imports"], cwd=cwd, env=env, capture_output=True, text=True)


def test_boundary_rules_hold():
    result = lint_imports(ROOT)
    assert result.returncode == 0, result.stdout
    assert "編排器只透過共同語言指揮 KEPT" in result.stdout
    assert "參與者不知道誰在指揮 KEPT" in result.stdout


@pytest.mark.parametrize(
    "where, line, message",
    [
        (
            "saga/machine.py",
            "from foodgo.kitchen.ticket import TicketStatus\n",
            "foodgo.saga is not allowed to import foodgo.kitchen",
        ),
        (
            "saga/orchestrator.py",
            "from foodgo.ordering.model import Order\n",
            "foodgo.saga is not allowed to import foodgo.ordering",
        ),
        (
            "payment/service.py",
            "from foodgo.saga.machine import SagaState\n",
            "foodgo.payment is not allowed to import foodgo.saga",
        ),
    ],
    ids=["saga-reads-kitchen", "saga-reads-ordering", "payment-knows-the-saga"],
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
def test_every_command_and_new_event_is_in_the_glossary():
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    for name in [*COMMANDS, *NEW_EVENTS, "Command"]:
        assert f"`{name}`" in glossary, f"{name} 要寫進 glossary.md"


@pytest.mark.parametrize(
    "term",
    [
        "編排器",
        "OrderSaga",
        "命令 topic",
        "payment.commands",
        "kitchen.commands",
        "delivery.commands",
        "ordering.commands",
        "sagas",
        "saga_log",
        "needs_attention",
        "逾時",
        "重試",
        "FOODGO_FAULT_REFUND",
        "FOODGO_SAGA",
        "FOODGO_SAGA_TIMEOUT",
        "slow",
        "人工處理",
    ],
)
def test_glossary_explains_the_orchestration(term):
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    assert term in glossary
