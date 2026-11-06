"""第 8 集示範：編排式 Saga——編排器送命令、收回覆；失敗時倒回去補償。每一步都記在 saga_log。

    uv run python demos/ep08_orchestration.py              # happy：一路走到外送員指派
    uv run python demos/ep08_orchestration.py decline      # 付款失敗 → 直接取消訂單
    uv run python demos/ep08_orchestration.py reject       # 付款成功、餐廳拒單 → 退款 → 取消訂單
    uv run python demos/ep08_orchestration.py crash        # 廚房當機：逾時 → 重試 → 補償；修好重啟後廚房單也撤銷
    uv run python demos/ep08_orchestration.py slow         # 廚房很慢：逾時 → 重試 → 回覆到了，照樣走完
    uv run python demos/ep08_orchestration.py refund-fail  # 餐廳拒單＋退款失敗 → 重試 → needs_attention（人工處理）
    uv run python demos/ep08_orchestration.py log A002     # 只看：A002 的 saga_log、sagas 與各上下文的表

要先 docker compose up -d。用 API 的資料庫（foodgo）與正式的 topic，跑完在 Kafka UI 看得到。
每跑一次下一張新的單（A001、A002…）。自己開著的 relay、consumer 先停掉（Ctrl-C）。
等回覆的秒數用 FOODGO_SAGA_TIMEOUT（預設 3）、重試次數 FOODGO_SAGA_RETRIES（預設 1）。
"""

import os
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import timedelta, timezone

import psycopg
from fastapi.testclient import TestClient

from foodgo import contracts
from foodgo.app import create_postgres_app
from foodgo.delivery.postgres import SCHEMAS as DELIVERY
from foodgo.infra import config, db, kafka
from foodgo.kitchen.postgres import SCHEMAS as KITCHEN
from foodgo.ordering.event_sourcing import SCHEMAS as ORDERING
from foodgo.ordering.event_sourcing import EventSourcedOrderRepository
from foodgo.payment.postgres import SCHEMAS as PAYMENT
from foodgo.saga.postgres import SCHEMAS as SAGA

TAIPEI = timezone(timedelta(hours=8))
A001 = {
    "customer_id": "C042",
    "restaurant_id": "R007",
    "lines": [{"name": "牛肉麵", "unit_price": 160, "quantity": 1}, {"name": "紅茶", "unit_price": 40, "quantity": 1}],
    "delivery_fee": 30,
    "note": "不要香菜",
    "delivery_address": "信義路五段 7 號",
}
PROGRAMS = {  # 平常是六個終端機各跑一個；這裡一次起好
    "relay": "foodgo.ordering.relay",
    "點餐": "foodgo.ordering.consumer",
    "編排器": "foodgo.saga.consumer",
    "付款": "foodgo.payment.consumer",
    "廚房": "foodgo.kitchen.consumer",
    "外送": "foodgo.delivery.consumer",
}
FINISHED = ("completed", "cancelled", "needs_attention")

# 每個情境：(標題, 哪個程序加什麼故障開關)
SCENARIOS = {
    "happy": ("一路走完：編排器一步一步送命令", {}),
    "decline": ("付款失敗：付款 consumer 加 FOODGO_FAULT_PAYMENT=decline", {"付款": {"FOODGO_FAULT_PAYMENT": "decline"}}),
    "reject": ("付款成功、餐廳拒單：廚房 consumer 加 FOODGO_FAULT_KITCHEN=reject", {"廚房": {"FOODGO_FAULT_KITCHEN": "reject"}}),
    "crash": ("廚房當機：廚房 consumer 加 FOODGO_FAULT_KITCHEN=crash", {"廚房": {"FOODGO_FAULT_KITCHEN": "crash"}}),
    "slow": ("廚房很慢：廚房 consumer 加 FOODGO_FAULT_KITCHEN=slow（每則想 4 秒）", {"廚房": {"FOODGO_FAULT_KITCHEN": "slow"}}),
    "refund-fail": (
        "退款也失敗：廚房加 FOODGO_FAULT_KITCHEN=reject、付款加 FOODGO_FAULT_REFUND=fail",
        {"廚房": {"FOODGO_FAULT_KITCHEN": "reject"}, "付款": {"FOODGO_FAULT_REFUND": "fail"}},
    ),
}


# ---------------------------------------------------------------- 排版：中文字佔兩格
def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, size: int) -> str:
    return text + " " * max(0, size - width(text))


# ---------------------------------------------------------------- 起程序、下單、等
def start_one(name: str, faults: dict[str, str] | None = None) -> subprocess.Popen:
    env = {**os.environ, "FOODGO_SAGA": "orchestration", **(faults or {})}
    return subprocess.Popen([sys.executable, "-m", PROGRAMS[name]], env=env)


def start(faults: dict[str, dict[str, str]]) -> dict[str, subprocess.Popen]:
    url = config.database_url()
    db.create_tables(url, *ORDERING, *PAYMENT, *KITCHEN, *DELIVERY, *SAGA)  # 先建好，六個程序同時起來也不會撞
    for topic in (
        contracts.ORDERING_TOPIC,
        contracts.PAYMENT_TOPIC,
        contracts.KITCHEN_TOPIC,
        contracts.DELIVERY_TOPIC,
        *contracts.COMMAND_TOPICS.values(),
    ):
        kafka.ensure_topic(topic)
    programs = {name: start_one(name, faults.get(name)) for name in PROGRAMS}
    time.sleep(4)  # 等五個 consumer 加入自己的 consumer group
    return programs


def stop(programs: dict[str, subprocess.Popen]) -> None:
    time.sleep(1.5)  # 讓最後一則送完、位置提交完
    for program in programs.values():
        if program.poll() is None:
            program.send_signal(signal.SIGINT)  # 和按 Ctrl-C 一樣：離開 consumer group 再結束
    for program in programs.values():
        program.wait(timeout=20)


def place_order() -> str:
    with TestClient(create_postgres_app()) as client:  # 和 POST /orders 走同一段程式，不用另外開 API
        order = client.post("/orders", json=A001).json()
    print(f"\n下單 → {order['order_id']} {order['status']}，合計 {order['total']} 元\n", flush=True)
    return order["order_id"]


def wait_for(query: str, order_id: str, expected: tuple[str, ...], seconds: float = 60) -> str | None:
    """等到查出來的值是 expected 其中之一；回傳那個值（逾時回 None）。"""
    deadline = time.monotonic() + seconds
    with psycopg.connect(config.database_url(), autocommit=True) as conn:
        while time.monotonic() < deadline:
            row = conn.execute(query, (order_id,)).fetchone()
            if row is not None and row[0] in expected:
                return row[0]
            time.sleep(0.3)
    return None


def scenario(name: str) -> None:
    title, faults = SCENARIOS[name]
    print(f"== {title} ==", flush=True)
    programs = start(faults)
    try:
        order_id = place_order()
        state = wait_for("SELECT state FROM sagas WHERE saga_id = %s", order_id, FINISHED)
        print(f"\n編排器：{order_id} → {state or '60 秒內沒走完'}\n", flush=True)
        if name == "crash":
            revive_kitchen(programs, order_id)
        if name == "slow":  # 重送的那一則，廚房想完也會回覆：等它進 saga_log（重複的，略過）
            wait_for("SELECT action FROM saga_log WHERE saga_id = %s ORDER BY id DESC LIMIT 1", order_id, ("ignore",), 15)
    finally:
        stop(programs)
    log(order_id)


def revive_kitchen(programs: dict[str, subprocess.Popen], order_id: str) -> None:
    """廚房修好了（拿掉 FOODGO_FAULT_KITCHEN）重新啟動：排在 kitchen.commands 的命令照順序處理。"""
    programs["廚房"].wait(timeout=20)  # 當掉的那一個
    print("== 廚房修好了，重新啟動（沒有故障開關）==\n", flush=True)
    programs["廚房"] = start_one("廚房")
    wait_for("SELECT status FROM tickets WHERE order_id = %s", order_id, ("cancelled",), seconds=30)
    wait_for(
        "SELECT message FROM saga_log WHERE saga_id = %s ORDER BY id DESC LIMIT 1", order_id, ("TicketCancelled",), 15
    )


# ---------------------------------------------------------------- saga_log 與各上下文的表
def log(order_id: str, *, url: str | None = None) -> None:
    """A002 走到哪了？看 sagas 一列就知道；怎麼走到這裡的，看 saga_log。"""
    url = url or config.database_url()
    with psycopg.connect(url) as conn:
        saga = conn.execute(
            "SELECT state, waiting_for, attempts, reason FROM sagas WHERE saga_id = %s", (order_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT id, at, state, action, message, detail FROM saga_log WHERE saga_id = %s ORDER BY id", (order_id,)
        ).fetchall()
        payment = conn.execute("SELECT payment_id, status, amount, reason FROM payments WHERE order_id = %s", (order_id,)).fetchone()
        ticket = conn.execute("SELECT ticket_id, status FROM tickets WHERE order_id = %s", (order_id,)).fetchone()
        delivery = conn.execute(
            "SELECT delivery_id, status, courier_id FROM deliveries WHERE order_id = %s", (order_id,)
        ).fetchone()
    print(f"== {order_id} 的 saga（sagas 一列）==")
    if saga is None:
        print("  （沒有：編排器沒聽到這張訂單）")
        return
    state, waiting_for, attempts, reason = saga
    waiting = f"，等 {waiting_for}（第 {attempts} 次）" if waiting_for else ""
    print(f"  state {state}{waiting}{f'；理由：{reason}' if reason else ''}")

    print(f"\n== saga_log（{len(rows)} 列）==")
    print(f"  {pad('id', 3)} {pad('時間', 12)}  {pad('state', 16)}  {pad('action', 10)}  {pad('message', 17)}  detail")
    for number, at, row_state, action, message, detail in rows:
        clock = at.astimezone(TAIPEI).strftime("%H:%M:%S.%f")[:-3]
        print(f"  {pad(str(number), 3)} {clock}  {pad(row_state, 16)}  {pad(action, 10)}  {pad(message, 17)}  {detail}")

    order = EventSourcedOrderRepository(url).load(order_id).state
    print("\n== 各上下文自己的表 ==")
    print(f"  點餐  events      {f'第 {order.version} 版 {order.status.value}' if order else '（沒有）'}")
    if payment is None:
        print("  付款  payments    （沒有）")
    else:
        payment_id, status, amount, why = payment
        print(f"  付款  payments    {payment_id} {status} {amount} 元{f'（{why}）' if why else ''}")
    print(f"  廚房  tickets     {' '.join(ticket) if ticket else '（沒有）'}")
    print(f"  外送  deliveries  {' '.join(x for x in delivery if x) if delivery else '（沒有）'}")


def main(args: list[str]) -> None:
    if args[:1] == ["log"] and len(args) == 2:
        log(args[1])
    elif len(args) <= 1 and (not args or args[0] in SCENARIOS):
        scenario(args[0] if args else "happy")
    else:
        sys.exit(f"用法：python demos/ep08_orchestration.py [{'|'.join(SCENARIOS)}|log 訂單編號]")


if __name__ == "__main__":
    main(sys.argv[1:])
