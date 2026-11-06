"""第 8 集：真的走 Kafka 與 PostgreSQL——編排器與四個參與者同時在跑，saga_log 記下每一步與每個補償。

執行（這個檔全部要 Docker）：
  docker compose up -d && uv run pytest tests/ep08 -q
用臨時的 topic 與 consumer group（測完刪掉）。編排器等回覆的時間設得很短（2～3 秒），一個測試十幾秒。
"""
import importlib.util
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foodgo.app import create_app
from foodgo.delivery import consumer as delivery_consumer
from foodgo.delivery.postgres import PostgresDeliveryRepository
from foodgo.delivery.service import Dispatch
from foodgo.infra import kafka, outbox
from foodgo.kitchen import consumer as kitchen_consumer
from foodgo.kitchen.postgres import PostgresTicketRepository
from foodgo.kitchen.service import Kitchen
from foodgo.ordering import consumer as ordering_consumer
from foodgo.ordering.event_sourcing import EventSourcedOrderRepository
from foodgo.payment import consumer as payment_consumer
from foodgo.payment.fakepay import FakePay
from foodgo.payment.gateway import FakePayAdapter
from foodgo.payment.postgres import PostgresPaymentRepository
from foodgo.payment.service import Payments
from foodgo.saga import consumer as saga_consumer
from foodgo.saga.machine import Settings
from foodgo.saga.orchestrator import Orchestrator
from foodgo.saga.postgres import PostgresSagaRepository

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]

A001 = {
    "customer_id": "C042",
    "restaurant_id": "R007",
    "lines": [
        {"name": "牛肉麵", "unit_price": 160, "quantity": 1},
        {"name": "紅茶", "unit_price": 40, "quantity": 1},
    ],
    "delivery_fee": 30,
    "note": "不要香菜",
    "delivery_address": "信義路五段 7 號",
}


def place_a001(db_url: str, t) -> None:
    """下單（API）＋ relay 程序做的事：三則事實到點餐的 topic。"""
    client = TestClient(create_app(orders=EventSourcedOrderRepository(db_url, topic=t.ordering)))
    assert client.post("/orders", json=A001).status_code == 201
    outbox.relay_once(db_url, kafka.KafkaSender())


class Cluster:
    """五個 consumer 各一條執行緒，像五個程序同時在跑。編排器閒了 idle 秒就結束；參與者跟著它一起下班。"""

    def __init__(self, db_url: str, t, group, *, timeout: float = 10, idle: float = 6) -> None:
        self.url, self.t, self.group, self.idle = db_url, t, group, idle
        commands = {
            "ordering": t.ordering_commands,
            "payment": t.payment_commands,
            "kitchen": t.kitchen_commands,
            "delivery": t.delivery_commands,
        }
        # "saga"：編排器自己公告的事件（第 9 集起放棄時公告 SagaEscalated）也送到臨時 topic，不碰正式的 saga.events
        self.sagas = PostgresSagaRepository(db_url, topics={**commands, "saga": t.saga})
        self.orchestrator = Orchestrator(self.sagas, Settings(timeout=timeout, retries=1))
        self.groups = {name: group() for name in ("saga", "ordering", "payment", "kitchen", "delivery")}

    def runners(self, *, payment_fault="", refund_fault="", kitchen_fault=""):
        url, t, g, idle = self.url, self.t, self.groups, 2  # 參與者每閒 2 秒看一次：編排器還在不在
        pay = Payments(
            FakePayAdapter(FakePay(fault=payment_fault, refund_fault=refund_fault)),
            PostgresPaymentRepository(url, topic=t.payment),
        )
        cook = Kitchen(tickets=PostgresTicketRepository(url, topic=t.kitchen), fault=kitchen_fault)
        send = Dispatch(PostgresDeliveryRepository(url, topic=t.delivery))
        orders = EventSourcedOrderRepository(url, topic=t.ordering)
        return {
            "ordering": lambda: ordering_consumer.run(
                orders, url, listens=[t.ordering_commands], group=g["ordering"], stop_when_idle=idle
            ),
            "payment": lambda: payment_consumer.run(
                pay, url, listens=[t.payment_commands], group=g["payment"], stop_when_idle=idle
            ),
            "kitchen": lambda: kitchen_consumer.run(
                cook, url, listens=[t.kitchen_commands], group=g["kitchen"], stop_when_idle=idle
            ),
            "delivery": lambda: delivery_consumer.run(
                send, url, listens=[t.delivery_commands], group=g["delivery"], stop_when_idle=idle
            ),
            "saga": lambda: saga_consumer.run(
                self.orchestrator,
                url,
                listens=[t.ordering, t.payment, t.kitchen, t.delivery],
                group=g["saga"],
                stop_when_idle=self.idle,
            ),
        }

    def run(self, *, only=None, without=(), **faults) -> None:
        """參與者先起來、站好位置，編排器再起來；全部閒下來才回來。"""
        runners = self.runners(**faults)
        names = [n for n in (only or runners) if n not in without]
        saga_done = threading.Event()

        def saga() -> None:
            try:
                runners["saga"]()
            finally:
                saga_done.set()

        def participant(run) -> None:
            while True:  # 閒了就重新加入 consumer group 再聽，直到編排器結束
                run()
                if saga_done.is_set():
                    return

        threads = {
            n: threading.Thread(target=saga if n == "saga" else participant, args=() if n == "saga" else (runners[n],))
            for n in names
        }
        for name, thread in threads.items():
            if name != "saga":
                thread.start()
        if "saga" in threads:
            time.sleep(3)  # 參與者加入 consumer group 要一點時間：不然第一則命令會因為還沒人收而逾時
            threads["saga"].start()
        for thread in threads.values():
            thread.join(timeout=120)

    def log(self, order_id: str = "A001") -> list[tuple[str, str]]:
        return [(e.action, e.message) for e in self.sagas.log(order_id)]


def types(messages) -> list[str]:
    return [dict(m.headers())["type"].decode() for m in messages]


def load_demo():
    spec = importlib.util.spec_from_file_location("ep08_orchestration", ROOT / "demos" / "ep08_orchestration.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 一路走完
def test_a001_goes_all_the_way_under_the_orchestrator(db_url, sql, saga_topics, group, messages_in, capsys):
    t = saga_topics
    place_a001(db_url, t)
    cluster = Cluster(db_url, t, group)
    cluster.run()

    assert sql("SELECT saga_id, state, waiting_for, reason FROM sagas") == [("A001", "completed", "", "")]
    assert [row for row in cluster.log() if row[0] != "receive"] == [
        ("send", "AuthorizePayment"),
        ("send", "AcceptTicket"),
        ("send", "AssignCourier"),
        ("finish", ""),
    ]
    assert sql("SELECT payment_id, status FROM payments") == [("P001", "authorized")]
    assert sql("SELECT ticket_id, status FROM tickets") == [("T001", "accepted")]
    assert sql("SELECT delivery_id, status, courier_id FROM deliveries") == [("D001", "assigned", "M01")]
    for table in ["outbox", "saga_outbox", "payment_outbox", "kitchen_outbox", "delivery_outbox"]:
        assert sql(f"SELECT count(*) FROM {table} WHERE sent_at IS NULL") == [(0,)], table

    # 命令走命令 topic，回覆走各自的事件 topic；key 都是 A001
    assert types(messages_in(t.payment_commands)) == ["AuthorizePayment"]
    assert types(messages_in(t.kitchen_commands)) == ["AcceptTicket"]
    assert types(messages_in(t.delivery_commands)) == ["AssignCourier"]
    assert messages_in(t.ordering_commands) == []
    assert types(messages_in(t.payment)) == ["PaymentAuthorized"]
    assert types(messages_in(t.kitchen)) == ["TicketAccepted"]
    assert types(messages_in(t.delivery)) == ["CourierAssigned"]

    demo = load_demo()
    capsys.readouterr()
    demo.log("A001", url=db_url)
    out = capsys.readouterr().out
    for words in ["state completed", "saga_log（10 列）", "AuthorizePayment", "整條流程走完", "D001 assigned M01"]:
        assert words in out, words


# ---------------------------------------------------------------- 付款成功、餐廳拒單：退款、取消，OrderCancelled 對外公告
def test_a_rejected_ticket_is_refunded_and_cancelled(db_url, sql, saga_topics, group, messages_in):
    t = saga_topics
    place_a001(db_url, t)
    cluster = Cluster(db_url, t, group)
    cluster.run(kitchen_fault="reject")

    assert sql("SELECT state, reason FROM sagas") == [("cancelled", "餐廳拒單：餐廳太忙，暫停接單")]
    assert [row for row in cluster.log() if row[0] in ("compensate", "finish")] == [
        ("compensate", "RefundPayment"),
        ("compensate", "CancelOrder"),
        ("finish", ""),
    ]
    assert sql("SELECT status FROM payments") == [("refunded",)]
    assert sql("SELECT status FROM tickets") == [("rejected",)]
    assert sql("SELECT count(*) FROM deliveries") == [(0,)]  # 外送根本沒被叫到
    assert sql("SELECT version, type, data->>'reason' FROM events WHERE type = 'OrderCancelled'") == [
        (5, "OrderCancelled", "餐廳拒單：餐廳太忙，暫停接單")
    ]
    assert types(messages_in(t.ordering)) == ["OrderPlaced", "PaymentRequested", "DeliveryRequested", "OrderCancelled"]
    assert types(messages_in(t.payment)) == ["PaymentAuthorized", "PaymentRefunded"]
    assert types(messages_in(t.ordering_commands)) == ["CancelOrder"]


# ---------------------------------------------------------------- 廚房沒回應：逾時、重試、補償；修好重啟後廚房單也撤銷
def test_a_silent_kitchen_times_out_and_is_compensated(db_url, sql, saga_topics, group, messages_in):
    t = saga_topics
    place_a001(db_url, t)
    cluster = Cluster(db_url, t, group, timeout=2)
    cluster.run(without=("kitchen",))  # 廚房當掉了：沒有人收 kitchen.commands

    assert sql("SELECT state, reason FROM sagas") == [("cancelled", "廚房沒有回覆：送了 2 次，各等 2 秒")]
    log = cluster.log()
    assert log.count(("timeout", "AcceptTicket")) == 2 and ("retry", "AcceptTicket") in log
    assert ("compensate", "CancelTicket") in log and ("compensate", "RefundPayment") in log
    assert sql("SELECT status FROM payments") == [("refunded",)]
    assert types(messages_in(t.kitchen_commands)) == ["AcceptTicket", "AcceptTicket", "CancelTicket"]

    cluster.run(only=("kitchen", "saga"))  # 廚房修好重啟：照順序先接單、再撤銷
    assert sql("SELECT status FROM tickets") == [("cancelled",)]
    assert sql("SELECT state FROM sagas") == [("cancelled",)]
    assert cluster.log()[-3:] == [("ignore", "TicketAccepted"), ("ignore", "TicketAccepted"), ("ignore", "TicketCancelled")]


# ---------------------------------------------------------------- 退款也失敗：重試、人工處理
def test_a_refund_that_keeps_failing_needs_attention(db_url, sql, saga_topics, group):
    t = saga_topics
    place_a001(db_url, t)
    cluster = Cluster(db_url, t, group)
    cluster.run(kitchen_fault="reject", refund_fault="fail")

    assert sql("SELECT state, waiting_for FROM sagas") == [("needs_attention", "")]
    assert cluster.log()[-5:] == [
        ("compensate", "RefundPayment"),
        ("receive", "RefundFailed"),
        ("retry", "RefundPayment"),
        ("receive", "RefundFailed"),
        ("give_up", ""),
    ]
    assert sql("SELECT status, reason FROM payments") == [("authorized", "退款失敗：發卡銀行暫時無法退款")]
    assert sql("SELECT count(*) FROM events WHERE type = 'OrderCancelled'") == [(0,)]  # 錢還沒退，不會默默取消
    assert sql("SELECT detail FROM saga_log WHERE action = 'give_up'") == [
        ("要人工處理：RefundPayment 試了 2 次都失敗（發卡銀行暫時無法退款）",)
    ]
