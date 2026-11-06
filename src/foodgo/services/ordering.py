"""點餐服務（第 10 集）：下單、查訂單、訂單追蹤。自己一個程序、自己一個資料庫（foodgo_ordering）：

    uv run python -m foodgo.services.ordering        # http://127.0.0.1:8101（顧客走 Gateway :8000）

HTTP：POST /orders、GET /orders/{id}（?as_of=）、GET /orders/{id}/events、POST /orders/{id}/cancel、
      GET /orders/{id}/tracking、GET /track/{id}；健康檢查 /healthz、/readyz。
背景工作（第 9 集一個終端機一個程序，現在是這個服務裡的四條執行緒）：
  relay     outbox → ordering.events
  ordering  聽 ordering.commands（CancelOrder）
  saga      編排器：聽四條 *.events，命令送到四條 *.commands；放棄時公告 saga.events
  tracking  投影：聽五條事件 topic → order_tracking
編排器（foodgo.saga）與讀取端（foodgo.tracking）是獨立的套件，只是跟著點餐服務一起部署：
它們的表不和點餐的表 JOIN、只靠 Kafka 拿資料；要搬成自己的服務，只要換一個資料庫連線。
"""
import os

from fastapi import FastAPI

from foodgo import contracts
from foodgo.infra import config, db, kafka, service
from foodgo.ordering import api as ordering_api
from foodgo.ordering import consumer as ordering_consumer
from foodgo.ordering import relay
from foodgo.ordering.event_sourcing import SCHEMAS as ORDERING
from foodgo.ordering.event_sourcing import EventSourcedOrderRepository
from foodgo.saga import consumer as saga_consumer
from foodgo.saga.machine import Settings
from foodgo.saga.orchestrator import Orchestrator
from foodgo.saga.postgres import SCHEMAS as SAGA
from foodgo.saga.postgres import PostgresSagaRepository
from foodgo.tracking import api as tracking_api
from foodgo.tracking import consumer as tracking_consumer
from foodgo.tracking.postgres import SCHEMAS as TRACKING
from foodgo.tracking.postgres import PostgresTrackingRepository

NAME = "ordering"
DATABASE = "foodgo_ordering"
SCHEMAS = (*ORDERING, *SAGA, *TRACKING)  # 這個資料庫裡的表：點餐、編排器、讀取端各自的
TOPICS = (  # 聽的與發的
    contracts.ORDERING_TOPIC,
    contracts.PAYMENT_TOPIC,
    contracts.KITCHEN_TOPIC,
    contracts.DELIVERY_TOPIC,
    contracts.SAGA_TOPIC,
    *contracts.COMMAND_TOPICS.values(),
)


def create_app(url: str | None = None) -> FastAPI:
    url = url or config.database_url(DATABASE)
    orders = EventSourcedOrderRepository(url)
    tracking = PostgresTrackingRepository(url)
    orchestrator = Orchestrator(PostgresSagaRepository(url), Settings.from_environment())

    def startup() -> None:
        db.ensure_database(url)
        db.create_tables(url, *SCHEMAS)
        for topic in TOPICS:
            kafka.ensure_topic(topic)

    return service.create_service(
        NAME,
        "點餐服務",
        database_url=url,
        routers=[ordering_api.build_router(orders), tracking_api.build_router(tracking)],
        workers=[
            service.Worker("relay", lambda stop: relay.run(url, stop=stop, fault=os.environ.get("FOODGO_FAULT_RELAY", ""))),
            service.Worker("ordering", lambda stop: ordering_consumer.run(orders, url, stop=stop)),
            service.Worker("saga", lambda stop: saga_consumer.run(orchestrator, url, stop=stop)),
            service.Worker("tracking", lambda stop: tracking_consumer.run(tracking, stop=stop)),
        ],
        on_startup=startup,
    )


def main() -> None:
    service.serve(NAME, create_app, database=DATABASE)


if __name__ == "__main__":
    main()
