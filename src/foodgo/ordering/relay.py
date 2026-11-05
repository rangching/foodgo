"""點餐的 relay：把 outbox 裡還沒送的事件送到 Kafka。和 API 分開，自己一個程序：

    uv run python -m foodgo.ordering.relay

FOODGO_FAULT_RELAY=crash-after-send：送到 Kafka 之後、標記 sent_at 之前當掉（示範重送）。
"""
import os
import time

from foodgo import contracts
from foodgo.infra import config, db, kafka, outbox
from foodgo.ordering.postgres import SCHEMAS


def main() -> None:
    url = config.database_url()
    db.create_tables(url, *SCHEMAS)
    kafka.ensure_topic(contracts.ORDERING_TOPIC)
    sender = kafka.KafkaSender()
    fault = os.environ.get("FOODGO_FAULT_RELAY", "")
    print(f"relay：每 0.5 秒看一次 outbox，送到 Kafka（{config.kafka_bootstrap()}）", flush=True)
    try:
        while True:
            if not outbox.relay_once(url, sender, fault=fault):
                time.sleep(0.5)  # 沒有要送的，等一下再看
    except KeyboardInterrupt:
        print("relay：停止", flush=True)


if __name__ == "__main__":
    main()
