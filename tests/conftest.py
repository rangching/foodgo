"""整合測試的共用工具（第 4 集起；第 6 集從 tests/ep04/conftest.py 搬到這裡，給每一集用）。

標了 @pytest.mark.integration 的測試要連 Kafka 與 PostgreSQL：先 docker compose up -d。
連不到就自動略過（skipped），不用 Docker 的單元測試照跑。

整合測試用自己的資料庫 foodgo_test 與每次不同名字的 topic，不會動到你練習用的資料。
"""
import socket
import uuid
from collections.abc import Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

import pytest

REASON = "連不到 Kafka／PostgreSQL：先 docker compose up -d（整合測試才需要）"


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: 需要 docker compose up -d（Kafka、PostgreSQL）")


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def services_up() -> bool:
    try:
        from foodgo.infra import config
    except ImportError:  # 起點還沒有 foodgo.infra
        return False
    pg = urlsplit(config.database_url())
    kafka_host, kafka_port = config.kafka_bootstrap().rsplit(":", 1)
    return _reachable(pg.hostname or "127.0.0.1", pg.port or 5432) and _reachable(kafka_host, int(kafka_port))


def pytest_collection_modifyitems(config, items):
    integration = [item for item in items if "integration" in item.keywords]
    if integration and not services_up():
        for item in integration:
            item.add_marker(pytest.mark.skip(reason=REASON))


# ---------------------------------------------------------------- PostgreSQL
@pytest.fixture(scope="session")
def test_db_url() -> str:
    import psycopg

    from foodgo.infra import config

    url = config.database_url()
    parts = urlsplit(url)
    with psycopg.connect(url, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = 'foodgo_test'").fetchone() is None:
            conn.execute("CREATE DATABASE foodgo_test")
    return urlunsplit(parts._replace(path="/foodgo_test"))


@pytest.fixture
def db_url(test_db_url: str) -> str:
    """每個測試都從空的 foodgo_test 開始，表已經建好。"""
    import psycopg

    from foodgo.infra import db
    from foodgo.kitchen.postgres import SCHEMAS as KITCHEN
    from foodgo.ordering.postgres import SCHEMAS as ORDERING

    try:
        from foodgo.ordering.event_sourcing import SCHEMAS as EVENT_SOURCING  # 第 6 集：events、snapshots
    except ImportError:  # 第 6 集起點還沒有
        EVENT_SOURCING = ()
    try:
        from foodgo.delivery.postgres import SCHEMAS as DELIVERY  # 第 7 集：付款、外送自己的表
        from foodgo.payment.postgres import SCHEMAS as PAYMENT
    except ImportError:  # 第 7 集起點還沒有
        DELIVERY = PAYMENT = ()

    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    db.create_tables(test_db_url, *ORDERING, *EVENT_SOURCING, *KITCHEN, *PAYMENT, *DELIVERY)
    return test_db_url


@pytest.fixture
def sql(db_url: str) -> Callable[..., list[tuple]]:
    """在 foodgo_test 跑一句 SQL，回傳所有列。"""
    import psycopg

    def run(query: str, *params: object) -> list[tuple]:
        with psycopg.connect(db_url) as conn:
            return conn.execute(query, params).fetchall()

    return run


# ---------------------------------------------------------------- Kafka
def _admin():
    from confluent_kafka.admin import AdminClient

    from foodgo.infra import config

    return AdminClient({"bootstrap.servers": config.kafka_bootstrap()})


@pytest.fixture
def topic() -> Iterator[str]:
    """每個測試一個新的 topic（3 個 partition），測完刪掉，Kafka UI 上不會留下來。"""
    from foodgo.infra import kafka

    name = f"test.ordering.{uuid.uuid4().hex[:8]}"
    kafka.ensure_topic(name)
    yield name
    admin = _admin()  # 要留著：admin 被回收的話，future 永遠等不到結果
    for future in admin.delete_topics([name]).values():
        future.result(timeout=30)


@pytest.fixture
def group() -> Iterator[Callable[[], str]]:
    """給 consumer group 取一個新名字，測完刪掉。"""
    names: list[str] = []

    def new() -> str:
        names.append(f"test.kitchen.{uuid.uuid4().hex[:8]}")
        return names[-1]

    yield new
    if names:
        admin = _admin()
        for future in admin.delete_consumer_groups(names).values():
            future.result(timeout=30)


@pytest.fixture
def read_topic() -> Iterator[Callable[[str], list]]:
    """從頭讀一個 topic 的所有訊息（不加入任何 consumer group）。"""
    from confluent_kafka import Consumer, TopicPartition

    from foodgo.infra import config

    consumers = []

    def read(name: str) -> list:
        consumer = Consumer(
            {"bootstrap.servers": config.kafka_bootstrap(), "group.id": "unused", "enable.auto.commit": False}
        )
        consumers.append(consumer)
        partitions = consumer.list_topics(name, timeout=10).topics[name].partitions
        consumer.assign([TopicPartition(name, p, 0) for p in partitions])  # 0＝從頭
        messages, empty = [], 0
        while empty < 3:
            message = consumer.poll(1.0)
            if message is None:
                empty += 1
            elif not message.error():
                messages.append(message)
        return sorted(messages, key=lambda m: m.timestamp()[1])

    yield read
    for consumer in consumers:
        consumer.close()
