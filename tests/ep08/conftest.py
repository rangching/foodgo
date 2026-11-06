"""第 8 集的 fixture。記憶體版的整條流程在 ep08_world.py；

整合測試的共用工具（foodgo_test 資料庫、group、沒開 Docker 自動略過）在 tests/conftest.py；
假的 Kafka 訊息 kafka_message、讀 topic 的 messages_in 在 tests/ep07/conftest.py（這裡載入來用）。
"""
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _ep07():
    spec = importlib.util.spec_from_file_location("ep07_conftest", ROOT / "tests" / "ep07" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_EP07 = _ep07()
kafka_message = _EP07.kafka_message  # FakeMessage(訊息, offset)：把命令或事件包成假的 Kafka 訊息
messages_in = _EP07.messages_in  # 一個 topic 裡現在有的所有訊息


@pytest.fixture
def saga_topics():
    """八個臨時 topic：四個事件 topic、四個命令 topic。測完刪掉。"""
    from confluent_kafka.admin import AdminClient

    from foodgo.infra import config, kafka

    suffix = uuid.uuid4().hex[:8]
    names = {}
    for ctx in ("ordering", "payment", "kitchen", "delivery"):
        names[ctx] = f"test.{ctx}.events.{suffix}"
        names[f"{ctx}_commands"] = f"test.{ctx}.commands.{suffix}"
    for name in names.values():
        kafka.ensure_topic(name)
    yield SimpleNamespace(**names)
    admin = AdminClient({"bootstrap.servers": config.kafka_bootstrap()})
    for future in admin.delete_topics(list(names.values())).values():
        future.result(timeout=30)
