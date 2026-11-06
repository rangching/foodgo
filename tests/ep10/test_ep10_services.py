"""第 10 集：依邊界拆服務——五個程序、各自的資料庫、各自的表；健康檢查；第 12～16 條邊界規則；compose 多了 Jaeger。

執行：uv run pytest tests/ep10 -q（這個檔不用 Docker）
"""
import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
CONTEXTS = ("ordering", "payment", "kitchen", "delivery")
NOWHERE = "postgresql://foodgo:foodgo@127.0.0.1:1/nowhere"  # 建 app 不會連資料庫：啟動（lifespan）時才連


def service(name: str):
    return importlib.import_module(f"foodgo.services.{name}")


def tables(schemas) -> set[str]:
    text = "\n".join(schemas)
    return set(re.findall(r"CREATE (?:TABLE|SEQUENCE) IF NOT EXISTS (\w+)", text))


# ---------------------------------------------------------------- 五個程序
def test_four_services_and_a_gateway():
    from foodgo.infra import config

    assert config.PORTS == {"gateway": 8000, "ordering": 8101, "payment": 8102, "kitchen": 8103, "delivery": 8104}
    for name in ("gateway", *CONTEXTS):
        module = service(name)
        assert module.NAME == name
        assert callable(module.create_app) and callable(module.main)  # python -m foodgo.services.<名字>
    assert not hasattr(service("gateway"), "DATABASE")  # Gateway 沒有資料庫


def test_each_service_has_its_own_database():
    names = [service(name).DATABASE for name in CONTEXTS]
    assert names == ["foodgo_ordering", "foodgo_payment", "foodgo_kitchen", "foodgo_delivery"]


def test_the_database_comes_from_the_environment(monkeypatch):
    from foodgo.infra import config

    monkeypatch.delenv("FOODGO_DATABASE_URL", raising=False)
    monkeypatch.delenv("FOODGO_POSTGRES_PORT", raising=False)
    assert config.database_url("foodgo_payment") == "postgresql://foodgo:foodgo@127.0.0.1:15432/foodgo_payment"
    assert config.database_url() == "postgresql://foodgo:foodgo@127.0.0.1:15432/foodgo"  # 第 4～9 集的單一資料庫照舊
    monkeypatch.setenv("FOODGO_DATABASE_URL", "postgresql://foodgo:foodgo@postgres:5432/foodgo_payment")  # 第 11 集
    assert config.database_url("foodgo_payment").startswith("postgresql://foodgo:foodgo@postgres:5432/")


# ---------------------------------------------------------------- 表的歸屬
def test_no_two_services_own_the_same_table():
    from foodgo.delivery.postgres import SCHEMAS as DELIVERY
    from foodgo.kitchen.postgres import SCHEMAS as KITCHEN
    from foodgo.payment.postgres import SCHEMAS as PAYMENT

    owned = {
        "ordering": tables(service("ordering").SCHEMAS),
        "payment": tables(PAYMENT),
        "kitchen": tables(KITCHEN),
        "delivery": tables(DELIVERY),
    }
    assert owned["ordering"] == {
        "order_number",
        "events",
        "snapshots",
        "outbox",
        "sagas",
        "saga_log",
        "saga_outbox",
        "processed_messages",
        "order_tracking",
        "projection_positions",
    }
    assert owned["payment"] == {"payment_number", "payments", "payment_outbox", "processed_messages"}
    assert owned["kitchen"] == {"ticket_number", "tickets", "kitchen_payments", "kitchen_outbox", "processed_messages"}
    assert owned["delivery"] == {"delivery_number", "deliveries", "delivery_tickets", "delivery_outbox", "processed_messages"}
    for a, mine in owned.items():
        for b, theirs in owned.items():
            if a < b:
                # processed_messages 是每個資料庫各一張（同名、不同庫），不是共用的
                assert mine & theirs <= {"processed_messages"}, (a, b)


def test_every_table_of_episode_9_has_an_owner():
    from foodgo.delivery.postgres import SCHEMAS as DELIVERY
    from foodgo.kitchen.postgres import SCHEMAS as KITCHEN
    from foodgo.ordering.event_sourcing import SCHEMAS as ORDERING
    from foodgo.payment.postgres import SCHEMAS as PAYMENT
    from foodgo.saga.postgres import SCHEMAS as SAGA
    from foodgo.tracking.postgres import SCHEMAS as TRACKING

    monolith = tables((*ORDERING, *PAYMENT, *KITCHEN, *DELIVERY, *SAGA, *TRACKING))  # 第 9 集全部在 foodgo 一個資料庫
    split = tables((*service("ordering").SCHEMAS, *PAYMENT, *KITCHEN, *DELIVERY))
    assert monolith == split


@pytest.mark.parametrize(
    "name, workers",
    [
        ("ordering", ["relay", "ordering", "saga", "tracking"]),
        ("payment", ["payment"]),
        ("kitchen", ["kitchen"]),
        ("delivery", ["delivery"]),
    ],
)
def test_the_eight_programs_became_workers(name, workers):
    app = service(name).create_app(NOWHERE)
    assert app.state.service == name
    assert [w.name for w in app.state.workers] == workers  # 第 9 集的七個 consumer／relay


@pytest.mark.parametrize(
    "name, has, has_not",
    [
        ("ordering", {"/orders", "/orders/{order_id}", "/orders/{order_id}/tracking", "/track/{order_id}"}, {"/tickets"}),
        ("kitchen", {"/tickets", "/tickets/{ticket_id}/ready"}, {"/orders"}),
        ("payment", set(), {"/orders", "/tickets"}),
        ("delivery", set(), {"/orders", "/tickets"}),
    ],
)
def test_each_service_serves_only_its_own_api(name, has, has_not):
    paths = set(TestClient(service(name).create_app(NOWHERE)).get("/openapi.json").json()["paths"])
    assert has | {"/healthz", "/readyz"} <= paths
    assert not paths & has_not


def test_services_do_not_call_each_other_over_http():
    src = ROOT / "src" / "foodgo"
    users = sorted(str(p.relative_to(src)) for p in src.rglob("*.py") if re.search(r"^\s*(import|from) httpx", p.read_text(encoding="utf-8"), re.MULTILINE))
    assert users == ["services/gateway.py"]  # 只有 Gateway 會發 HTTP 請求；服務之間只走 Kafka


# ---------------------------------------------------------------- 健康檢查
def test_healthz_is_ok_while_every_worker_runs():
    from foodgo.infra.service import Worker, create_service

    app = create_service("kitchen", "廚房服務", workers=[Worker("kitchen", lambda stop: stop.wait())])
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"service": "kitchen", "status": "ok", "workers": {"kitchen": "running"}}
        assert client.get("/readyz").json() == {"service": "kitchen", "status": "ready"}


def test_healthz_is_503_when_a_worker_dies():
    from foodgo.infra.service import Worker, create_service

    crashed = threading.Event()

    def crash(stop: threading.Event) -> None:
        crashed.set()
        raise RuntimeError("廚房當機")

    app = create_service("kitchen", "廚房服務", workers=[Worker("kitchen", crash), Worker("idle", lambda stop: stop.wait())])
    with TestClient(app) as client:
        crashed.wait(2)
        app.state.workers[0].join(2)
        response = client.get("/healthz")
        assert response.status_code == 503  # 第 11 集的 HEALTHCHECK、第 12 集的存活探針看到 503 就重開它
        assert response.json()["workers"] == {"kitchen": "stopped: RuntimeError: 廚房當機", "idle": "running"}


def test_workers_stop_when_the_service_stops():
    from foodgo.infra.service import Worker, create_service

    stopped = threading.Event()

    def work(stop: threading.Event) -> None:
        stop.wait()
        stopped.set()

    with TestClient(create_service("payment", "付款服務", workers=[Worker("payment", work)])):
        assert not stopped.is_set()
    assert stopped.is_set()  # Ctrl-C／SIGTERM：lifespan 收尾時叫背景工作停


def test_readyz_is_503_without_the_database():
    from foodgo.infra.service import create_service

    with TestClient(create_service("payment", "付款服務", database_url=NOWHERE)) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "no database"


# ---------------------------------------------------------------- 邊界：每個服務只組裝自己的上下文；Gateway 只認得網址
def lint_imports(cwd, env=None):
    return subprocess.run(["lint-imports"], cwd=cwd, env=env, capture_output=True, text=True)


def test_boundary_rules_hold():
    result = lint_imports(ROOT)
    assert result.returncode == 0, result.stdout
    for rule in [
        "點餐服務只組裝點餐、編排器與讀取端",
        "付款服務只組裝付款",
        "廚房服務只組裝廚房",
        "外送服務只組裝外送",
        "Gateway 只認得網址",
    ]:
        assert f"{rule} KEPT" in result.stdout
    assert "四個上下文彼此獨立 KEPT" in result.stdout  # 第 1 集那一條照樣守著


@pytest.mark.parametrize(
    "where, line, message",
    [
        (
            "services/payment.py",
            "from foodgo.kitchen.service import Kitchen\n",
            "foodgo.services.payment is not allowed to import foodgo.kitchen",
        ),
        (
            "services/ordering.py",
            "from foodgo.payment.payment import PaymentStatus\n",
            "foodgo.services.ordering is not allowed to import foodgo.payment",
        ),
        (
            "services/kitchen.py",
            "from foodgo.services import ordering\n",
            "foodgo.services.kitchen is not allowed to import foodgo.ordering",
        ),
        (
            "services/gateway.py",
            "from foodgo.ordering.api import OrderOut\n",
            "foodgo.services.gateway is not allowed to import foodgo.ordering",
        ),
        (
            "services/gateway.py",
            "from foodgo import contracts\n",
            "foodgo.services.gateway is not allowed to import foodgo.contracts",
        ),
    ],
    ids=["payment-runs-the-kitchen", "ordering-reads-payment", "kitchen-borrows-ordering", "gateway-knows-orders", "gateway-reads-events"],
)
def test_back_doors_are_caught(tmp_path, where, line, message):
    shutil.copytree(ROOT / "src", tmp_path / "src")
    shutil.copy(ROOT / ".importlinter", tmp_path)
    target = tmp_path / "src" / "foodgo" / where
    target.write_text(target.read_text(encoding="utf-8") + "\n" + line, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "src")}
    result = lint_imports(tmp_path, env)
    assert result.returncode == 1
    assert message in result.stdout


# ---------------------------------------------------------------- compose：多了 Jaeger
@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]


def test_jaeger_joins_the_compose(compose):
    assert set(compose) == {"kafka", "kafka-ui", "postgres", "jaeger"}
    image, tag = compose["jaeger"]["image"].rsplit(":", 1)
    assert image == "jaegertracing/jaeger"  # Jaeger v2（all-in-one 是 v1 的名字）
    assert re.fullmatch(r"2\.\d+\.\d+", tag), f"要釘到明確版本：{tag}"
    assert compose["jaeger"]["healthcheck"]["test"]


def test_jaeger_ports_can_be_changed(compose):
    assert compose["jaeger"]["ports"] == ["${FOODGO_JAEGER_UI_PORT:-16686}:16686", "${FOODGO_OTLP_PORT:-14318}:4318"]
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "FOODGO_JAEGER_UI_PORT=16686" in example and "FOODGO_OTLP_PORT=14318" in example


def test_python_defaults_match_compose(monkeypatch):
    from foodgo.infra import config

    for variable in ["OTEL_EXPORTER_OTLP_ENDPOINT", "FOODGO_OTLP_PORT", "FOODGO_JAEGER_UI_PORT", "FOODGO_JAEGER_URL"]:
        monkeypatch.delenv(variable, raising=False)
    assert config.otlp_endpoint() == "http://127.0.0.1:14318"
    assert config.jaeger_url() == "http://127.0.0.1:16686"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318")  # OpenTelemetry 的標準變數（第 11 集）
    assert config.otlp_endpoint() == "http://jaeger:4318"


# ---------------------------------------------------------------- 一鍵起五個程序
def load_demo():
    spec = importlib.util.spec_from_file_location("ep10_services", ROOT / "demos" / "ep10_services.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_launcher_gives_each_service_its_own_database(monkeypatch):
    monkeypatch.setenv("FOODGO_DATABASE_URL", "postgresql://foodgo:foodgo@127.0.0.1:15432/foodgo")  # 第 9 集留下來的也不會用到
    monkeypatch.delenv("FOODGO_POSTGRES_PORT", raising=False)
    demo = load_demo()
    assert [svc.NAME for svc in demo.SERVICES] == ["gateway", "ordering", "payment", "kitchen", "delivery"]
    assert "FOODGO_DATABASE_URL" not in demo.environment(service("gateway"), {})
    env = demo.environment(service("kitchen"), {"FOODGO_FAULT_KITCHEN": "reject"})
    assert env["FOODGO_DATABASE_URL"] == "postgresql://foodgo:foodgo@127.0.0.1:15432/foodgo_kitchen"
    assert env["FOODGO_FAULT_KITCHEN"] == "reject"  # 故障開關只給那一個服務


# ---------------------------------------------------------------- 詞彙表
@pytest.mark.parametrize(
    "term",
    [
        "模組化單體",
        "微服務",
        "API Gateway",
        "X-Correlation-ID",
        "關聯 ID",
        "OpenTelemetry",
        "traceparent",
        "span",
        "Jaeger",
        "foodgo_ordering",
        "foodgo_payment",
        "foodgo_kitchen",
        "foodgo_delivery",
        "/healthz",
        "/readyz",
        "foodgo.services",
    ],
)
def test_glossary_explains_the_split(term):
    glossary = (ROOT / "glossary.md").read_text(encoding="utf-8")
    assert term in glossary
