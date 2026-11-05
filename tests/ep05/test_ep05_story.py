"""第 5 集：CRUD 只留結果、事件留下過程——示範腳本與詞彙表。

執行：uv run pytest tests/ep05 -q（不用 Docker）
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "demos" / "ep05_history.py"


def run_demo(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(DEMO), *args], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout


def test_crud_keeps_only_the_last_row():
    out = run_demo("crud")
    assert "cancelled" in out
    assert "顧客改變心意" not in out  # orders 表沒有放理由的欄位：理由不見了


def test_the_event_stream_keeps_every_step():
    out = run_demo("events")
    for word in ["OrderDrafted", "ItemAdded", "OrderPlaced", "OrderCancelled", "顧客改變心意"]:
        assert word in out
    assert out.count("ItemAdded") == 2


def test_fold_replays_the_same_rows_crud_overwrote():
    out = run_demo("fold")
    assert "230 元" in out
    for status in ["draft", "placed", "cancelled"]:
        assert status in out


def test_two_cancels_at_once_only_one_lands():
    out = run_demo("race")
    assert "ConcurrencyConflict" in out
    assert "訂單已取消，不能再取消" in out  # 重讀之後，由 Order 的規則擋下


def test_the_demo_runs_end_to_end():
    out = run_demo()
    for title in ["CRUD", "事件", "fold", "樂觀併發"]:
        assert title in out


@pytest.mark.parametrize(
    "term",
    ["OrderDrafted", "ItemAdded", "evolve", "fold", "事件流", "版本", "expected_version", "樂觀併發", "ConcurrencyConflict", "不該用", "菜單"],
)
def test_glossary_explains_event_sourcing(term):
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    assert term in glossary
