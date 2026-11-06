"""廚房服務（第 10 集）：接單、出餐。自己一個程序、自己一個資料庫（foodgo_kitchen）：

    uv run python -m foodgo.services.kitchen         # http://127.0.0.1:8103（顧客與店家走 Gateway :8000）

HTTP：GET /tickets、POST /tickets/{id}/ready；健康檢查 /healthz、/readyz。
背景工作：kitchen 聽 kitchen.commands（AcceptTicket、CancelTicket），回覆公告到 kitchen.events。
故障開關照舊：FOODGO_FAULT_KITCHEN=reject／slow／crash（crash：背景工作停了，/healthz 變 503）。
"""
import os

from fastapi import FastAPI

from foodgo import contracts
from foodgo.infra import config, db, kafka, service
from foodgo.kitchen import api, consumer
from foodgo.kitchen.postgres import SCHEMAS, PostgresTicketRepository
from foodgo.kitchen.service import Kitchen

NAME = "kitchen"
DATABASE = "foodgo_kitchen"
TOPICS = (contracts.KITCHEN_COMMANDS, contracts.KITCHEN_TOPIC)  # 聽的與發的


def create_app(url: str | None = None) -> FastAPI:
    url = url or config.database_url(DATABASE)
    counter = Kitchen(tickets=PostgresTicketRepository(url))  # 出餐櫃台（HTTP）：沒有故障開關
    cooks = Kitchen(  # 接單的廚師（consumer）：故障開關在這裡
        tickets=PostgresTicketRepository(url),
        fault=os.environ.get("FOODGO_FAULT_KITCHEN", ""),
        slow_seconds=float(os.environ.get("FOODGO_KITCHEN_SLOW_SECONDS", "4")),
    )

    def startup() -> None:
        db.ensure_database(url)
        db.create_tables(url, *SCHEMAS)
        for topic in TOPICS:
            kafka.ensure_topic(topic)

    return service.create_service(
        NAME,
        "廚房服務",
        database_url=url,
        routers=[api.build_router(counter)],
        workers=[service.Worker("kitchen", lambda stop: consumer.run(cooks, url, stop=stop))],
        on_startup=startup,
    )


def main() -> None:
    service.serve(NAME, create_app, database=DATABASE)


if __name__ == "__main__":
    main()
