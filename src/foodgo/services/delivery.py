"""外送服務（第 10 集）：開配送單、指派外送員。自己一個程序、自己一個資料庫（foodgo_delivery）：

    uv run python -m foodgo.services.delivery        # http://127.0.0.1:8104（只有健康檢查）

背景工作：delivery 聽 delivery.commands（AssignCourier），回覆公告到 delivery.events。
"""
from fastapi import FastAPI

from foodgo import contracts
from foodgo.delivery import consumer
from foodgo.delivery.postgres import SCHEMAS, PostgresDeliveryRepository
from foodgo.delivery.service import Dispatch
from foodgo.infra import config, db, kafka, service

NAME = "delivery"
DATABASE = "foodgo_delivery"
TOPICS = (contracts.DELIVERY_COMMANDS, contracts.DELIVERY_TOPIC)  # 聽的與發的


def create_app(url: str | None = None) -> FastAPI:
    url = url or config.database_url(DATABASE)
    dispatch = Dispatch(PostgresDeliveryRepository(url))

    def startup() -> None:
        db.ensure_database(url)
        db.create_tables(url, *SCHEMAS)
        for topic in TOPICS:
            kafka.ensure_topic(topic)

    return service.create_service(
        NAME,
        "外送服務",
        database_url=url,
        workers=[service.Worker("delivery", lambda stop: consumer.run(dispatch, url, stop=stop))],
        on_startup=startup,
    )


def main() -> None:
    service.serve(NAME, create_app, database=DATABASE)


if __name__ == "__main__":
    main()
