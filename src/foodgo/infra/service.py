"""一個服務＝一個程序（第 10 集）：FastAPI（HTTP 與健康檢查）＋幾個背景工作（consumer、relay），一起起、一起停。

第 9 集一個終端機跑一個程序（八個）；拆成服務之後，同一個服務的 consumer 與 relay 變成這個程序裡的執行緒。
  /healthz：活著嗎——每個背景工作都還在跑就 200，有一個停了（例如當掉）就 503。第 11 集的 HEALTHCHECK、第 12 集的存活探針用它。
  /readyz ：可以接客了嗎——啟動做完、連得上自己的資料庫就 200。第 12 集的就緒探針用它。
"""
import threading
import traceback
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import psycopg
import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from foodgo.infra import config, telemetry

VERSION = "0.10.0"


@dataclass
class Worker:
    """一個背景工作：run(stop) 一直做，直到 stop 被設起來才回來。丟例外就停下來，錯誤記在 error。"""

    name: str
    run: Callable[[threading.Event], None]
    error: str = ""
    _thread: threading.Thread | None = field(default=None, repr=False)

    def start(self, stop: threading.Event) -> None:
        def body() -> None:
            try:
                self.run(stop)
            except Exception as error:  # noqa: BLE001 —— 停下來、記下來，讓 /healthz 回 503
                self.error = f"{type(error).__name__}: {error}"
                traceback.print_exc()

        self._thread = threading.Thread(target=body, name=self.name, daemon=True)
        self._thread.start()

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def state(self) -> str:
        if self._thread is None:
            return "not started"
        if self._thread.is_alive():
            return "running"
        return f"stopped: {self.error}" if self.error else "stopped"


def create_service(
    name: str,
    title: str,
    *,
    database_url: str | None = None,
    routers: Sequence[APIRouter] = (),
    workers: Sequence[Worker] = (),
    on_startup: Callable[[], None] | None = None,
) -> FastAPI:
    """組一個服務。啟動時：on_startup（建資料庫、建表、建 topic）→ 背景工作一個一個起來；關掉時：叫它們停、等它們收完。"""
    stop = threading.Event()
    ready = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if on_startup:
            on_startup()
        for worker in workers:
            worker.start(stop)
        ready.set()
        names = "、".join(w.name for w in workers) or "沒有"
        print(f"{name}：服務起來了（背景工作：{names}）", flush=True)
        yield
        stop.set()  # consumer 處理完手上那一則、relay 送完這一批，就會停
        for worker in workers:
            worker.join(timeout=15)
        print(f"{name}：服務停了", flush=True)

    app = FastAPI(title=f"好食快送 FoodGo：{title}", version=VERSION, lifespan=lifespan)
    app.state.service, app.state.workers = name, list(workers)
    for router in routers:
        app.include_router(router)

    @app.get("/healthz", tags=["健康檢查"], summary="活著嗎：背景工作都還在跑")
    def healthz() -> JSONResponse:
        states = {w.name: w.state for w in workers}
        ok = all(state == "running" for state in states.values()) or stop.is_set()
        body = {"service": name, "status": "ok" if ok else "failing", "workers": states}
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/readyz", tags=["健康檢查"], summary="可以接客了嗎：啟動做完、連得上資料庫")
    def readyz() -> JSONResponse:
        if not ready.is_set():
            return JSONResponse({"service": name, "status": "starting"}, status_code=503)
        if database_url:
            try:
                with psycopg.connect(database_url, connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
            except psycopg.Error as error:
                return JSONResponse({"service": name, "status": "no database", "error": str(error)}, status_code=503)
        return JSONResponse({"service": name, "status": "ready"})

    telemetry.instrument(app)
    return app


def serve(name: str, create_app: Callable[[], FastAPI], *, database: str = "") -> None:
    """python -m foodgo.services.<名字> 跑的是這個：設定追蹤（Jaeger 上叫 name）→ 起 uvicorn → 結束前把 span 送完。

    Ctrl-C（本機）或 SIGTERM（第 11 集 docker stop、第 12 集 K8S）都會走 lifespan 的收尾：背景工作停好才離開。
    """
    telemetry.setup(name)
    port = config.service_port(name)
    where = f"（資料庫 {database}）" if database else ""
    print(f"{name}：http://{config.service_host()}:{port}{where}", flush=True)
    try:
        uvicorn.run(create_app(), host=config.service_host(), port=port, log_level="warning")
    finally:
        telemetry.shutdown()
