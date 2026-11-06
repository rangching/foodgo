"""第 9 集示範：讀寫分家——下單後一直查訂單追蹤（讀模型），看它一格一格更新；砍掉讀模型、從事件重建。

    uv run python demos/ep09_tracking.py              # happy：下單後每 0.05 秒查一次 GET /orders/{id}/tracking
    uv run python demos/ep09_tracking.py slow         # 廚房想 2 秒才回：追蹤頁看得到一格一格亮起來（錄追蹤頁用）
    uv run python demos/ep09_tracking.py decline      # 付款失敗 → 已取消
    uv run python demos/ep09_tracking.py reject       # 付款成功、餐廳拒單 → 退款 → 已取消
    uv run python demos/ep09_tracking.py refund-fail  # 拒單＋退款失敗 → 人工處理中（編排器公告 SagaEscalated）
    uv run python demos/ep09_tracking.py rebuild      # 砍掉 order_tracking、從 Kafka 重播，比對前後的指紋
    uv run python demos/ep09_tracking.py show A003    # 只看：A003 在讀模型裡的樣子

要先 docker compose up -d。用 API 的資料庫（foodgo）與正式的 topic。每跑一個情境下一張新的單（A001、A002…）。
會自己起七個程序：relay、點餐、編排器、付款、廚房、外送（第 8 集那六個），加上這一集的投影。
自己開著的 relay、consumer 先停掉（Ctrl-C）；追蹤頁要另外開 uv run fastapi dev，再打開 http://127.0.0.1:8000/track/A001。
"""

import os
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import timedelta, timezone

from fastapi.testclient import TestClient

from foodgo import contracts
from foodgo.app import create_postgres_app
from foodgo.delivery.postgres import SCHEMAS as DELIVERY
from foodgo.infra import config, db, kafka
from foodgo.kitchen.postgres import SCHEMAS as KITCHEN
from foodgo.ordering.event_sourcing import SCHEMAS as ORDERING
from foodgo.payment.postgres import SCHEMAS as PAYMENT
from foodgo.saga.postgres import SCHEMAS as SAGA
from foodgo.tracking.postgres import SCHEMAS as TRACKING
from foodgo.tracking.postgres import PostgresTrackingRepository
from foodgo.tracking.projection import STATUS, headline, steps

TAIPEI = timezone(timedelta(hours=8))
A001 = {
    "customer_id": "C042",
    "restaurant_id": "R007",
    "lines": [{"name": "牛肉麵", "unit_price": 160, "quantity": 1}, {"name": "紅茶", "unit_price": 40, "quantity": 1}],
    "delivery_fee": 30,
    "note": "不要香菜",
    "delivery_address": "信義路五段 7 號",
}
PROGRAMS = {  # 平常是七個終端機各跑一個；這裡一次起好
    "relay": "foodgo.ordering.relay",
    "點餐": "foodgo.ordering.consumer",
    "編排器": "foodgo.saga.consumer",
    "付款": "foodgo.payment.consumer",
    "廚房": "foodgo.kitchen.consumer",
    "外送": "foodgo.delivery.consumer",
    "投影": "foodgo.tracking.consumer",
}
MARKS = {"done": "✓", "active": "…", "failed": "✕", "undone": "↺", "todo": "·", "skipped": "—"}

# 每個情境：(標題, 哪個程序加什麼故障開關, 等到哪個狀態才算走完)
SCENARIOS = {
    "happy": ("一路走完：下單後一直查追蹤", {}, "delivering"),
    "slow": (
        "廚房慢一點：廚房 consumer 加 FOODGO_FAULT_KITCHEN=slow、FOODGO_KITCHEN_SLOW_SECONDS=2",
        {"廚房": {"FOODGO_FAULT_KITCHEN": "slow", "FOODGO_KITCHEN_SLOW_SECONDS": "2"}},
        "delivering",
    ),
    "decline": ("付款失敗：付款 consumer 加 FOODGO_FAULT_PAYMENT=decline", {"付款": {"FOODGO_FAULT_PAYMENT": "decline"}}, "cancelled"),
    "reject": ("餐廳拒單：廚房 consumer 加 FOODGO_FAULT_KITCHEN=reject", {"廚房": {"FOODGO_FAULT_KITCHEN": "reject"}}, "cancelled"),
    "refund-fail": (
        "退款也失敗：廚房加 FOODGO_FAULT_KITCHEN=reject、付款加 FOODGO_FAULT_REFUND=fail",
        {"廚房": {"FOODGO_FAULT_KITCHEN": "reject"}, "付款": {"FOODGO_FAULT_REFUND": "fail"}},
        "needs_attention",
    ),
}


# ---------------------------------------------------------------- 起程序、下單
def pad(text: str, size: int) -> str:
    """中文字佔兩格：補空白補到 size 格寬。"""
    return text + " " * max(0, size - sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text))


def start_one(name: str, faults: dict[str, str] | None = None) -> subprocess.Popen:
    env = {**os.environ, "FOODGO_SAGA": "orchestration", **(faults or {})}
    quiet = None if name == "投影" else subprocess.DEVNULL  # 只留投影的紀錄，其他六個第 8 集看過了
    return subprocess.Popen([sys.executable, "-m", PROGRAMS[name]], env=env, stdout=quiet)


def start(faults: dict[str, dict[str, str]]) -> dict[str, subprocess.Popen]:
    url = config.database_url()
    db.create_tables(url, *ORDERING, *PAYMENT, *KITCHEN, *DELIVERY, *SAGA, *TRACKING)  # 先建好，七個程序同時起來也不會撞
    for topic in (
        contracts.ORDERING_TOPIC,
        contracts.PAYMENT_TOPIC,
        contracts.KITCHEN_TOPIC,
        contracts.DELIVERY_TOPIC,
        contracts.SAGA_TOPIC,
        *contracts.COMMAND_TOPICS.values(),
    ):
        kafka.ensure_topic(topic)
    programs = {name: start_one(name, faults.get(name)) for name in PROGRAMS}
    time.sleep(5)  # 等六個 consumer 加入自己的 consumer group（投影也會先把以前的事件讀完）
    return programs


def stop(programs: dict[str, subprocess.Popen]) -> None:
    time.sleep(1.5)  # 讓最後一則送完、位置記完
    for program in programs.values():
        if program.poll() is None:
            program.send_signal(signal.SIGINT)  # 和按 Ctrl-C 一樣
    for program in programs.values():
        program.wait(timeout=20)


def line(body: dict) -> str:
    """一次查詢的結果，排成一行：狀態＋四格。"""
    cells = "  ".join(f"{s['title']} {MARKS[s['state']]}" for s in body["steps"])
    return f"{pad(STATUS[body['status']], 10)}  {cells}   as_of_position {body['as_of_position']}"


def scenario(name: str) -> None:
    title, faults, final = SCENARIOS[name]
    print(f"== {title} ==", flush=True)
    programs = start(faults)
    try:
        with TestClient(create_postgres_app()) as client:  # 和 POST /orders、GET …/tracking 走同一段程式
            order = client.post("/orders", json=A001).json()
            began = time.monotonic()
            order_id = order["order_id"]
            print(f"\n下單 → {order_id} {order['status']}，合計 {order['total']} 元（寫模型：events 第 {order['version']} 版）")
            print(f"\n  經過     GET /orders/{order_id}/tracking")
            last = None
            while time.monotonic() - began < 60:
                response = client.get(f"/orders/{order_id}/tracking")
                body = response.json()
                if response.status_code == 404:
                    now = ("404",)
                    text = f"404  更新中…（as_of_position {body['as_of_position']}：投影還沒讀到 {order_id}）"
                else:
                    now = (body["status"], tuple(s["state"] for s in body["steps"]))
                    text = f"200  {line(body)}"
                if now != last:
                    print(f"  {time.monotonic() - began:5.2f} 秒  {text}", flush=True)
                    last = now
                if response.status_code == 200 and body["status"] == final:
                    break
                time.sleep(0.05)
    finally:
        stop(programs)
    print()
    show(order_id)
    print(f"\n追蹤頁（另一個終端機 uv run fastapi dev）：http://127.0.0.1:8000/track/{order_id}")


# ---------------------------------------------------------------- 讀模型裡的一列
def show(order_id: str, *, url: str | None = None) -> None:
    tracking = PostgresTrackingRepository(url or config.database_url())
    t = tracking.get(order_id)
    if t is None:
        print(f"== {order_id}：讀模型裡還沒有（投影沒讀到，或還沒開投影 consumer）==")
        return
    print(f"== {order_id} {STATUS[t.status]}（order_tracking 一列）==")
    print(f"  {headline(t)}")
    for step in steps(t):
        print(f"  {step.title}  {MARKS[step.state]} {step.detail}".rstrip())
    menu = "、".join(f"{i.name} ×{i.quantity}" for i in t.items) or "（品項還在路上）"
    amount = f"{t.amount} 元" if t.amount is not None else "金額還在路上"
    print(f"  {menu}；備註：{t.note or '無'}；{amount}；送到 {t.delivery_address or '—'}")
    when = t.updated_at.astimezone(TAIPEI).strftime("%H:%M:%S")
    print(f"  資料時間 {when}（最晚那一則事件發生的時間）；as_of_position {tracking.as_of_position()}")


def main(args: list[str]) -> None:
    if args[:1] == ["show"] and len(args) == 2:
        show(args[1])
    elif args == ["rebuild"]:
        from foodgo.tracking import rebuild

        rebuild.main()
    elif len(args) <= 1 and (not args or args[0] in SCENARIOS):
        scenario(args[0] if args else "happy")
    else:
        sys.exit(f"用法：python demos/ep09_tracking.py [{'|'.join(SCENARIOS)}|rebuild|show 訂單編號]")


if __name__ == "__main__":
    main(sys.argv[1:])
