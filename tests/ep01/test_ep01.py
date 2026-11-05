"""第 1 集：四個限界上下文、Event Storming 貼出的 11 個事件、2 條邊界規則。

執行：uv run pytest tests/ep01 -q
"""
import dataclasses
import importlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Event Storming 時間軸上的橘色便利貼，依上下文分組
EVENTS = {
    "ordering": {"OrderPlaced", "OrderCancelled"},
    "payment": {"PaymentAuthorized", "PaymentFailed", "PaymentRefunded"},
    "kitchen": {"TicketAccepted", "TicketRejected", "MealReady"},
    "delivery": {"CourierAssigned", "MealPickedUp", "MealDelivered"},
}


def lint_imports(cwd, env=None):
    return subprocess.run(["lint-imports"], cwd=cwd, env=env, capture_output=True, text=True)


@pytest.mark.parametrize("context", EVENTS)
def test_context_says_what_it_cares_about(context):
    module = importlib.import_module(f"foodgo.{context}")
    assert module.__doc__ and module.__doc__.strip()


@pytest.mark.parametrize("context", EVENTS)
def test_events_are_immutable_facts(context):
    from foodgo.shared.events import DomainEvent

    module = importlib.import_module(f"foodgo.{context}.events")
    found = {
        name for name, obj in vars(module).items()
        if isinstance(obj, type) and issubclass(obj, DomainEvent) and obj is not DomainEvent
    }
    assert found == EVENTS[context]
    for name in found:
        cls = getattr(module, name)
        assert dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen, f"{name} 要是 frozen dataclass"
        assert cls.__doc__ and not cls.__doc__.startswith(name), f"{name} 要用通用語言寫一句說明"


def test_glossary_names_the_order_in_each_context():
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    for term in ["訂單", "付款", "廚房單", "配送單"]:
        assert term in glossary


def test_boundaries_hold():
    result = lint_imports(ROOT)
    assert result.returncode == 0, result.stdout
    assert "Contracts: 2 kept, 0 broken." in result.stdout


def test_crossing_a_boundary_is_caught(tmp_path):
    shutil.copytree(ROOT / "src", tmp_path / "src")
    shutil.copy(ROOT / ".importlinter", tmp_path)
    leak = tmp_path / "src" / "foodgo" / "kitchen" / "leak.py"
    leak.write_text("from foodgo.ordering.events import OrderPlaced\n", encoding="utf-8")

    env = {**os.environ, "PYTHONPATH": str(tmp_path / "src")}
    result = lint_imports(tmp_path, env)
    assert result.returncode == 1
    assert "foodgo.kitchen is not allowed to import foodgo.ordering" in result.stdout
