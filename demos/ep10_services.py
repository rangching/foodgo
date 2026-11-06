"""第 10 集示範：拆成服務——一次起五個程序（Gateway＋四個服務），從 Gateway 下單，在 Jaeger 看一整條 trace。

    uv run python demos/ep10_services.py up           # 只起五個程序、印每個程序的紀錄（Ctrl-C 全部停）；另開終端機自己 curl
    uv run python demos/ep10_services.py              # happy：起五個程序 → 從 Gateway 下單 → 查追蹤到走完 → 去 Jaeger 拿這條 trace
    uv run python demos/ep10_services.py reject       # 餐廳拒單 → 退款 → 取消：還是同一條 trace
    uv run python demos/ep10_services.py decline      # 付款失敗 → 取消
    uv run python demos/ep10_services.py trace <trace_id>   # 只看：Jaeger 上這條 trace 的每一段

要先 docker compose up -d（第 10 集多了 jaeger）。每個服務用自己的資料庫（foodgo_ordering、foodgo_payment…，第一次啟動自己建）
與正式的 topic；每跑一個情境下一張新的單（A001、A002…）。第 9 集自己開著的程序與 fastapi dev 先停掉（Ctrl-C）：
它們用同樣的 consumer group 名字，和這一集的服務一起跑會互相搶訊息。
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import httpx

from foodgo.infra import config, kafka
from foodgo.services import delivery, gateway, kitchen, ordering, payment

SERVICES = [gateway, ordering, payment, kitchen, delivery]  # 起的順序；Gateway 不需要資料庫
GROUPS = ("ordering", "saga", "tracking", "payment", "kitchen", "delivery")  # 六個 consumer group 都有人了才下單
A001 = {
    "customer_id": "C042",
    "restaurant_id": "R007",
    "lines": [{"name": "牛肉麵", "unit_price": 160, "quantity": 1}, {"name": "紅茶", "unit_price": 40, "quantity": 1}],
    "delivery_fee": 30,
    "note": "不要香菜",
    "delivery_address": "信義路五段 7 號",
}
# 每個情境：(標題, 哪個服務加什麼故障開關, 等到哪個狀態才算走完)
SCENARIOS = {
    "happy": ("一路走完：A001 從 Gateway 進來", {}, "delivering"),
    "reject": ("餐廳拒單：廚房服務加 FOODGO_FAULT_KITCHEN=reject", {"kitchen": {"FOODGO_FAULT_KITCHEN": "reject"}}, "cancelled"),
    "decline": ("付款失敗：付款服務加 FOODGO_FAULT_PAYMENT=decline", {"payment": {"FOODGO_FAULT_PAYMENT": "decline"}}, "cancelled"),
}


# ---------------------------------------------------------------- 起五個程序
def environment(svc, faults: dict[str, str]) -> dict[str, str]:
    """每個程序自己的環境變數：自己的資料庫（第 11 集寫進 compose.yaml 的就是這幾個）。"""
    env = {**os.environ, **faults}
    env.pop("FOODGO_DATABASE_URL", None)
    if hasattr(svc, "DATABASE"):
        env["FOODGO_DATABASE_URL"] = config.postgres_url(svc.DATABASE)
    return env


def start(faults: dict[str, dict[str, str]], *, loud: bool) -> dict[str, subprocess.Popen]:
    programs = {}
    for svc in SERVICES:
        program = subprocess.Popen(
            [sys.executable, "-m", svc.__name__],
            env=environment(svc, faults.get(svc.NAME, {})),
            stdout=subprocess.PIPE if loud else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if loud else None,
            text=True,
            start_new_session=True,  # 終端機的 Ctrl-C 只給這個腳本；由 stop() 一個一個叫它們收尾
        )
        if loud:  # 像 honcho：每一行前面標上是哪個程序
            threading.Thread(target=echo, args=(svc.NAME, program), daemon=True).start()
        programs[svc.NAME] = program
    return programs


def echo(name: str, program: subprocess.Popen) -> None:
    for text in program.stdout:
        print(f"{name:>8} │ {text.rstrip()}", flush=True)


def wait_until_up(programs: dict[str, subprocess.Popen], *, timeout: float = 60) -> float:
    """等五個 /healthz 都 200，六個 consumer group 都有人加入。回傳等了幾秒。"""
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        dead = [name for name, program in programs.items() if program.poll() is not None]
        if dead:
            sys.exit(f"{'、'.join(dead)} 沒起來：看上面的錯誤（埠被占用？docker compose up -d 了嗎？）")
        try:
            healthy = all(httpx.get(f"{config.service_url(svc.NAME)}/healthz", timeout=1).status_code == 200 for svc in SERVICES)
        except httpx.TransportError:
            healthy = False
        if healthy and all(kafka.active_members(group) for group in GROUPS):
            return time.monotonic() - began
        time.sleep(0.5)
    sys.exit("等了一分鐘還沒全部起來")


def stop(programs: dict[str, subprocess.Popen]) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):  # 收尾時再按一次 Ctrl-C 也不要打斷
        signal.signal(sig, signal.SIG_IGN)
    for program in programs.values():
        if program.poll() is None:
            program.send_signal(signal.SIGINT)  # 和按 Ctrl-C 一樣：uvicorn 收尾、背景工作停好、span 送完
    for program in programs.values():
        program.wait(timeout=30)


def ports() -> str:
    return "、".join(f"{svc.NAME} :{config.service_port(svc.NAME)}" for svc in SERVICES)


# ---------------------------------------------------------------- 情境
def scenario(name: str) -> None:
    title, faults, final = SCENARIOS[name]
    print(f"== {title} ==", flush=True)
    print(f"起五個程序：{ports()}", flush=True)
    programs = start(faults, loud=False)
    try:
        waited = wait_until_up(programs)
        print(f"  五個 /healthz 都 200、{len(GROUPS)} 個 consumer group 都有人了（{waited:.1f} 秒）\n", flush=True)
        base = config.service_url("gateway")
        with httpx.Client(base_url=base, timeout=10) as client:
            response = client.post("/orders", json=A001)
            order = response.json()
            order_id, trace_id = order["order_id"], response.headers["x-trace-id"]
            print(f"POST {base}/orders → {response.status_code} {order_id} {order['status']}，合計 {order['total']} 元")
            print(f"  X-Correlation-ID: {response.headers['x-correlation-id']}")
            print(f"  X-Trace-ID:       {trace_id}")
            status = follow(client, order_id, final)
        print(f"\n追蹤（經過 Gateway）：{order_id} → {status}")
    finally:
        stop(programs)  # 先停：每個程序結束前把手上的 span 送完
    print()
    show_trace(trace_id)


def follow(client: httpx.Client, order_id: str, final: str) -> str:
    """一直查 GET /orders/{id}/tracking（走 Gateway → 點餐服務的讀取端），狀態一變就印一行。"""
    began, last, status = time.monotonic(), None, ""
    print(f"\n  經過     GET /orders/{order_id}/tracking（Gateway → 點餐服務）")
    while time.monotonic() - began < 60:
        response = client.get(f"/orders/{order_id}/tracking")
        body = response.json()
        status = body.get("status", "404")
        now = (response.status_code, status)
        if now != last:
            text = "更新中…" if response.status_code == 404 else f"{body['status_label']}：{body['headline']}"
            print(f"  {time.monotonic() - began:5.2f} 秒  {response.status_code}  {text}", flush=True)
            last = now
        if status == final:
            return status
        time.sleep(0.1)
    return status


# ---------------------------------------------------------------- Jaeger
def fetch_trace(trace_id: str, *, settle: float = 3.0, timeout: float = 30) -> dict | None:
    """問 Jaeger 要這條 trace；span 還在路上（每個程序每秒送一批），就等到數量不再變。"""
    began, count, stable_since, data = time.monotonic(), -1, time.monotonic(), None
    while time.monotonic() - began < timeout:
        try:
            response = httpx.get(f"{config.jaeger_url()}/api/traces/{trace_id}", timeout=5)
        except httpx.TransportError:
            sys.exit(f"連不上 Jaeger（{config.jaeger_url()}）：docker compose up -d 了嗎？")
        if response.status_code == 200 and response.json().get("data"):
            data = response.json()["data"][0]
            if len(data["spans"]) != count:
                count, stable_since = len(data["spans"]), time.monotonic()
            elif time.monotonic() - stable_since >= settle:
                return data
        time.sleep(0.5)
    return data


def show_trace(trace_id: str) -> None:
    data = fetch_trace(trace_id)
    if data is None:
        print(f"Jaeger 上還沒有 {trace_id}：程序還沒把 span 送出去，或 Jaeger 重開過（資料只放記憶體）")
        return
    services = {pid: p["serviceName"] for pid, p in data["processes"].items()}
    spans = data["spans"]
    by_id = {s["spanID"]: s for s in spans}
    children: dict[str | None, list[dict]] = {}
    for s in spans:
        parent = next((r["spanID"] for r in s.get("references", []) if r["refType"] == "CHILD_OF"), None)
        children.setdefault(parent if parent in by_id else None, []).append(s)
    start = min(s["startTime"] for s in spans)
    count = {svc.NAME: 0 for svc in SERVICES}
    for s in spans:
        count[services[s["processID"]]] = count.get(services[s["processID"]], 0) + 1
    count = {name: n for name, n in count.items() if n}
    print(f"Jaeger：{config.jaeger_url()}/trace/{trace_id}")
    print(f"  1 條 trace、{len(count)} 個服務、{len(spans)} 個 span（" + "、".join(f"{k} {v}" for k, v in count.items()) + "）")
    reads = sum(1 for s in spans if s["operationName"].startswith("tracking process"))
    print(f"  下面省略讀取端（tracking process …）的 {reads} 個 span：它聽每一則事件，每則一個\n")
    print(f"  {'開始':>8}  {'服務':<9} span")

    def walk(span: dict, depth: int) -> None:
        name = span["operationName"]
        if not name.startswith("tracking process"):
            error = any(t["key"] == "error" and t["value"] for t in span.get("tags", []))
            at = (span["startTime"] - start) / 1_000_000
            service = services[span["processID"]]
            print(f"  {at:7.3f}s  {service:<9} {'  ' * depth}{name}{'  ✕ 出錯' if error else ''}")
        for child in sorted(children.get(span["spanID"], []), key=lambda s: s["startTime"]):
            walk(child, depth + 1)

    for root in sorted(children.get(None, []), key=lambda s: s["startTime"]):
        walk(root, 0)


# ---------------------------------------------------------------- 只起程序
def up() -> None:
    print(f"起五個程序：{ports()}（Ctrl-C 全部停）", flush=True)
    programs = start({}, loud=True)
    try:
        waited = wait_until_up(programs)
        base = config.service_url("gateway")
        print(f"\n五個都好了（{waited:.1f} 秒）。另開一個終端機：", flush=True)
        print(f"  curl -s {base}/services | uv run python -m json.tool")
        print(f"  curl -si {base}/orders -H 'Content-Type: application/json' -d '{json.dumps(A001, ensure_ascii=False)}'")
        print(f"  追蹤頁 {base}/track/A001　　Jaeger {config.jaeger_url()}\n", flush=True)
        while all(program.poll() is None for program in programs.values()):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop(programs)


def interrupted(*_) -> None:
    """Ctrl-C 或被 kill（SIGTERM）：只算第一次（uv 會再轉送一次），照樣收尾，五個程序一個都不留。"""
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)
    raise KeyboardInterrupt


def main(args: list[str]) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    if args == ["up"]:
        up()
    elif args[:1] == ["trace"] and len(args) == 2:
        show_trace(args[1])
    elif len(args) <= 1 and (not args or args[0] in SCENARIOS):
        scenario(args[0] if args else "happy")
    else:
        sys.exit(f"用法：python demos/ep10_services.py [up|{'|'.join(SCENARIOS)}|trace <trace_id>]")


if __name__ == "__main__":
    main(sys.argv[1:])
