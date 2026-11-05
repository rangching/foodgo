"""第 3 集：共同語言 foodgo.contracts——整合事件的 JSON 格式、版本，以及「放什麼、不放什麼」。

執行：uv run pytest tests/ep03 -q
"""
import dataclasses
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from foodgo import contracts

ROOT = Path(__file__).resolve().parents[2]


def a001_placed() -> contracts.OrderPlaced:
    return contracts.OrderPlaced(
        order_id="A001",
        restaurant_id="R007",
        items=(
            contracts.OrderItem(name="牛肉麵", quantity=1),
            contracts.OrderItem(name="紅茶", quantity=1),
        ),
        note="不要香菜",
    )


# ---------------------------------------------------------------- 格式就是契約
def test_order_placed_json_is_the_contract():
    envelope = json.loads(a001_placed().to_json())
    assert set(envelope) == {"type", "schema_version", "event_id", "occurred_at", "data"}
    assert envelope["type"] == "OrderPlaced"
    assert envelope["schema_version"] == 1
    assert envelope["data"] == {
        "order_id": "A001",
        "restaurant_id": "R007",
        "items": [{"name": "牛肉麵", "quantity": 1}, {"name": "紅茶", "quantity": 1}],
        "note": "不要香菜",
    }


def test_the_kitchen_gets_no_prices():
    fields = {f.name for f in dataclasses.fields(contracts.OrderPlaced)}
    assert fields == {"event_id", "occurred_at", "order_id", "restaurant_id", "items", "note"}
    assert {f.name for f in dataclasses.fields(contracts.OrderItem)} == {"name", "quantity"}
    data = json.dumps(json.loads(a001_placed().to_json())["data"])
    for word in ["price", "total", "fee", "160", "230", "customer", "address"]:
        assert word not in data, f"OrderPlaced 不該帶 {word}"


def test_metadata_is_plain_json_text():
    event = a001_placed()
    assert isinstance(event.event_id, str)
    assert datetime.fromisoformat(event.occurred_at).tzinfo is not None  # 帶時區的 ISO 8601


def test_integration_events_are_immutable():
    event = a001_placed()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.note = "加辣"  # type: ignore[misc]


def test_json_round_trip_gives_an_equal_event():
    sent = a001_placed()
    received = contracts.OrderPlaced.from_json(sent.to_json())
    assert received == sent
    assert received is not sent
    assert isinstance(received.items[0], contracts.OrderItem)


# ---------------------------------------------------------------- 版本
def test_unknown_schema_version_is_refused():
    envelope = json.loads(a001_placed().to_json())
    envelope["schema_version"] = 2
    with pytest.raises(ValueError, match="版"):
        contracts.OrderPlaced.from_json(json.dumps(envelope))


def test_a_different_event_type_is_refused():
    with pytest.raises(ValueError):
        contracts.MealReady.from_json(a001_placed().to_json())


def test_new_fields_are_ignored_by_old_readers():
    sent = a001_placed()
    envelope = json.loads(sent.to_json())
    envelope["data"]["eta_minutes"] = 15  # 之後加的欄位：舊的訂閱者看不懂，先略過
    assert contracts.OrderPlaced.from_json(json.dumps(envelope)) == sent


def test_meal_ready_contract():
    event = contracts.MealReady(order_id="A001", ticket_id="T001", restaurant_id="R007")
    envelope = json.loads(event.to_json())
    assert (envelope["type"], envelope["schema_version"]) == ("MealReady", 1)
    assert envelope["data"] == {"order_id": "A001", "ticket_id": "T001", "restaurant_id": "R007"}
    assert contracts.MealReady.from_json(event.to_json()) == event


# ---------------------------------------------------------------- 邊界
def test_contracts_need_nothing_but_the_standard_library():
    code = (
        "import sys, foodgo.contracts; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in {'fastapi', 'pydantic'} "
        "or (m.startswith('foodgo.') and not m.startswith('foodgo.contracts'))))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


def test_glossary_explains_what_goes_into_an_event():
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    for term in ["命令", "整合事件", "領域事件", "Published Language", "schema_version", "不放"]:
        assert term in glossary


def lint_imports(cwd, env=None):
    return subprocess.run(["lint-imports"], cwd=cwd, env=env, capture_output=True, text=True)


def test_contracts_rule_holds():
    result = lint_imports(ROOT)
    assert result.returncode == 0, result.stdout
    assert "共同語言不認識任何上下文 KEPT" in result.stdout


@pytest.mark.parametrize(
    "where, line, message",
    [
        (
            "contracts",
            "from foodgo.kitchen.ticket import Ticket\n",
            "foodgo.contracts is not allowed to import foodgo.kitchen",
        ),
        (
            "ordering",
            "from foodgo.kitchen.service import Kitchen\n",
            "foodgo.ordering is not allowed to import foodgo.kitchen",
        ),
    ],
    ids=["contracts-knows-kitchen", "ordering-calls-kitchen"],
)
def test_back_doors_are_caught(tmp_path, where, line, message):
    shutil.copytree(ROOT / "src", tmp_path / "src")
    shutil.copy(ROOT / ".importlinter", tmp_path)
    (tmp_path / "src" / "foodgo" / where / "leak.py").write_text(line, encoding="utf-8")

    env = {**os.environ, "PYTHONPATH": str(tmp_path / "src")}
    result = lint_imports(tmp_path, env)
    assert result.returncode == 1
    assert message in result.stdout
