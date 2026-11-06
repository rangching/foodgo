"""點餐的 relay：把 outbox 裡還沒送的事件送到 Kafka。和 API 分開，自己一個程序：

    uv run python -m foodgo.ordering.relay

FOODGO_FAULT_RELAY=crash-after-send：送到 Kafka 之後、標記 sent_at 之前當掉（示範重送）。
第 10 集起也是點餐服務裡的一個背景工作（run(url, stop=…)）。
"""
import os
import threading

from foodgo import contracts
from foodgo.infra import config, db, kafka, outbox
from foodgo.ordering.event_sourcing import SCHEMAS


def run(url: str, *, stop: threading.Event | None = None, fault: str = "") -> None:
    """每 0.5 秒看一次 outbox，有就送；stop 設起來就結束。"""
    sender = kafka.KafkaSender()
    stop = stop or threading.Event()
    while not stop.is_set():
        if not outbox.relay_once(url, sender, fault=fault):
            stop.wait(0.5)  # 沒有要送的，等一下再看


def main() -> None:
    url = config.database_url()
    db.create_tables(url, *SCHEMAS)
    kafka.ensure_topic(contracts.ORDERING_TOPIC)
    print(f"relay：每 0.5 秒看一次 outbox，送到 Kafka（{config.kafka_bootstrap()}）", flush=True)
    try:
        run(url, fault=os.environ.get("FOODGO_FAULT_RELAY", ""))
    except KeyboardInterrupt:
        print("relay：停止", flush=True)


if __name__ == "__main__":
    main()
