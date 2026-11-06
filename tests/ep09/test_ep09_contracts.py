"""第 9 集：讀取端的位置——誰聽什麼、存在哪、編排器公告「轉人工處理」、第 10、11 條邊界規則、詞彙表。

執行：uv run pytest tests/ep09 -q（這個檔不用 Docker）
"""
import dataclasses
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from foodgo import contracts

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- 編排器公告「轉人工處理」
def test_the_orchestrator_has_its_own_events_topic():
    assert contracts.SAGA_TOPIC == "saga.events"  # 和別人一樣：<發布者>.events


def test_saga_escalated_is_a_version_1_event():
    event = contracts.SagaEscalated(order_id="A001", reason="RefundPayment 試了 2 次都失敗（發卡銀行暫時無法退款）")
    assert issubclass(contracts.SagaEscalated, contracts.IntegrationEvent)
    assert not issubclass(contracts.SagaEscalated, contracts.Command)
    envelope = json.loads(event.to_json())
    assert envelope["schema_version"] == 1
    assert envelope["data"] == {"order_id": "A001", "reason": "RefundPayment 試了 2 次都失敗（發卡銀行暫時無法退款）"}
    assert {f.name for f in dataclasses.fields(contracts.SagaEscalated)} - {"event_id", "occurred_at"} == {
        "order_id",
        "reason",
    }
    assert contracts.SagaEscalated.from_json(event.to_json()) == event


def test_giving_up_announces_saga_escalated():
    from ep08_world import World

    world = World(kitchen_fault="reject", refund_fault="fail")
    world.place()
    assert world.saga.state.value == "needs_attention"
    [escalated] = world.sagas.announced
    assert isinstance(escalated, contracts.SagaEscalated)
    assert (escalated.order_id, escalated.reason) == ("A001", "RefundPayment 試了 2 次都失敗（發卡銀行暫時無法退款）")
    assert all(isinstance(command, contracts.Command) for _, command in world.sagas.sent)  # 命令還是命令


@pytest.mark.parametrize("faults", [{}, {"payment_fault": "decline"}, {"kitchen_fault": "reject"}])
def test_the_orchestrator_announces_nothing_else(faults):
    from ep08_world import World

    world = World(**faults)
    world.place()
    assert world.sagas.announced == []  # 其他事實都由參與者公告了


def test_pull_still_returns_only_commands_and_log():
    from ep08_world import World

    world = World(kitchen_fault="reject", refund_fault="fail")
    world.place()
    saga = world.saga
    assert saga.pull() == ([], []) and saga.pull_announcements() == []  # 存檔時都取走了


# ---------------------------------------------------------------- 誰聽什麼、存在哪
def test_the_projection_listens_to_every_events_topic():
    from foodgo.tracking import consumer

    assert consumer.GROUP == "tracking"
    assert consumer.LISTENS == ("ordering.events", "payment.events", "kitchen.events", "delivery.events", "saga.events")
    assert not any(topic.endswith(".commands") for topic in consumer.LISTENS)  # 讀取端只聽事實，不聽命令


def test_the_read_model_tables():
    from foodgo.tracking.postgres import SCHEMAS

    text = "\n".join(SCHEMAS)
    for table in ["order_tracking", "projection_positions"]:
        assert f"CREATE TABLE IF NOT EXISTS {table} " in text, table
    for column in ["status", "payment", "kitchen", "delivery", "courier_id", "attention", "updated_at", "last_offset"]:
        assert column in text, column
    assert "processed_messages" not in text  # 讀到哪記在 projection_positions，不靠 event_id


def test_the_saga_routes_its_announcement_to_its_own_topic():
    from foodgo.saga.postgres import TOPICS

    assert TOPICS == {**contracts.COMMAND_TOPICS, "saga": "saga.events"}


def test_the_app_mounts_the_read_side():
    from fastapi.testclient import TestClient

    from foodgo.app import create_app

    paths = TestClient(create_app()).get("/openapi.json").json()["paths"]
    assert {"/orders/{order_id}/tracking", "/track/{order_id}"} <= set(paths)


# ---------------------------------------------------------------- 邊界：讀取端只吃共同語言；寫入端不知道誰在讀
def lint_imports(cwd, env=None):
    return subprocess.run(["lint-imports"], cwd=cwd, env=env, capture_output=True, text=True)


def test_boundary_rules_hold():
    result = lint_imports(ROOT)
    assert result.returncode == 0, result.stdout
    assert "讀模型只吃共同語言 KEPT" in result.stdout
    assert "寫入端不知道誰在讀 KEPT" in result.stdout


@pytest.mark.parametrize(
    "where, line, message",
    [
        (
            "tracking/projection.py",
            "from foodgo.payment.payment import PaymentStatus\n",
            "foodgo.tracking is not allowed to import foodgo.payment",
        ),
        (
            "tracking/postgres.py",
            "from foodgo.saga.machine import SagaState\n",
            "foodgo.tracking is not allowed to import foodgo.saga",
        ),
        (
            "ordering/api.py",
            "from foodgo.tracking.projection import Tracking\n",
            "foodgo.ordering is not allowed to import foodgo.tracking",
        ),
    ],
    ids=["tracking-reads-payment", "tracking-reads-the-saga", "ordering-knows-the-reader"],
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
@pytest.mark.parametrize(
    "term",
    [
        "CQRS",
        "寫模型",
        "讀模型",
        "投影",
        "order_tracking",
        "projection_positions",
        "最終一致",
        "as_of_position",
        "冪等",
        "重建",
        "`SagaEscalated`",
        "saga.events",
        "人工處理中",
        "foodgo.tracking.rebuild",
        "/track/",
    ],
)
def test_glossary_explains_the_read_side(term):
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    assert term in glossary
