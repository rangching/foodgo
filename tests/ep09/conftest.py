"""第 9 集的 fixture。A001 各情境的事實在 ep09_facts.py；

記憶體版的整條流程（World）在 tests/ep08/ep08_world.py（這裡把那個資料夾放進 sys.path，測試檔直接 from ep08_world import …）；
八個臨時 topic saga_topics（第 9 集多一條 saga）與讀 topic 的 messages_in 在 tests/ep08/conftest.py（這裡載入來用）。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "ep08"))


def _ep08():
    spec = importlib.util.spec_from_file_location("ep08_conftest", ROOT / "tests" / "ep08" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_EP08 = _ep08()
saga_topics = _EP08.saga_topics  # 四條事件 topic＋四條命令 topic＋編排器的 saga（臨時的，測完刪掉）
messages_in = _EP08.messages_in


@pytest.fixture
def send():
    """把事件送到 Kafka 的某個 topic（key＝order_id，和 outbox relay 送的一模一樣）。"""
    from foodgo.infra import kafka

    sender = kafka.KafkaSender()

    def put(topic: str, *events) -> None:
        for event in events:
            sender.send(topic, event.order_id, event.to_json(), {"event_id": event.event_id, "type": type(event).__name__})
        sender.flush()

    return put
