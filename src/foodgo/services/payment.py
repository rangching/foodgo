"""付款服務（第 10 集）：向金流服務要授權、退款。自己一個程序、自己一個資料庫（foodgo_payment）：

    uv run python -m foodgo.services.payment         # http://127.0.0.1:8102（只有健康檢查：顧客不直接找付款）

背景工作：payment 聽 payment.commands（AuthorizePayment、RefundPayment），回覆公告到 payment.events。
故障開關照舊：FOODGO_FAULT_PAYMENT=decline、FOODGO_FAULT_REFUND=fail。
"""
from fastapi import FastAPI

from foodgo import contracts
from foodgo.infra import config, db, kafka, service
from foodgo.payment import consumer, gateway
from foodgo.payment.postgres import SCHEMAS, PostgresPaymentRepository
from foodgo.payment.service import Payments

NAME = "payment"
DATABASE = "foodgo_payment"
TOPICS = (contracts.PAYMENT_COMMANDS, contracts.PAYMENT_TOPIC)  # 聽的與發的


def create_app(url: str | None = None) -> FastAPI:
    url = url or config.database_url(DATABASE)
    payments = Payments(gateway.from_environment(), PostgresPaymentRepository(url))

    def startup() -> None:
        db.ensure_database(url)
        db.create_tables(url, *SCHEMAS)
        for topic in TOPICS:
            kafka.ensure_topic(topic)

    return service.create_service(
        NAME,
        "付款服務",
        database_url=url,
        workers=[service.Worker("payment", lambda stop: consumer.run(payments, url, stop=stop))],
        on_startup=startup,
    )


def main() -> None:
    service.serve(NAME, create_app, database=DATABASE)


if __name__ == "__main__":
    main()
