"""第 11 集：compose.yaml 一鍵起全部——基礎設施四個＋FoodGo 五個容器（profile app）；詞彙表。

執行：uv run pytest tests/ep11 -q（這個檔不用 Docker；最後一個要有 docker 指令，daemon 不用開）
"""
import importlib
import json
import os
import shutil
import subprocess

import pytest
from ep11_files import APPS, INFRA, ROOT

CONTEXTS = ("ordering", "payment", "kitchen", "delivery")
SWITCHES = {  # 第 8～10 集的故障開關與調整（每個只給一個服務）
    "FOODGO_FAULT_RELAY",
    "FOODGO_SAGA_TIMEOUT",
    "FOODGO_SAGA_RETRIES",
    "FOODGO_FAULT_PAYMENT",
    "FOODGO_FAULT_REFUND",
    "FOODGO_FAULT_KITCHEN",
    "FOODGO_KITCHEN_SLOW_SECONDS",
}


def module(name: str):
    return importlib.import_module(f"foodgo.services.{name}")


# ---------------------------------------------------------------- 兩種起法
def test_infrastructure_starts_without_a_profile(compose):
    assert set(compose) == {*INFRA, *APPS}
    for name in INFRA:
        assert "profiles" not in compose[name], name  # docker compose up -d：照舊只起這四個（第 4～10 集）
    for name in APPS:
        assert compose[name]["profiles"] == ["app"], name  # --profile app 才起


def test_one_image_five_containers(compose):
    from foodgo.infra.service import VERSION

    for name in APPS:
        svc = compose[name]
        assert svc["image"] == f"foodgo:{VERSION}"  # 映像檔的 tag 跟著程式版本
        assert svc["build"] == "."  # 同一個 Dockerfile
        assert svc["command"] == ["python", "-m", f"foodgo.services.{name}"]  # 只差啟動哪一個
        assert svc["restart"] == "unless-stopped"
        assert "healthcheck" not in svc  # 用 Dockerfile 的 HEALTHCHECK
    # 第 12 集：不再寫死 0.11.0——滾動更新把 VERSION 改成 0.12.0，compose 的 image 照樣要跟著它


# ---------------------------------------------------------------- 環境變數：第 10 集的那些，換成容器裡的位址
def environment(compose, name: str) -> dict[str, str]:
    return {key: str(value) for key, value in compose[name]["environment"].items()}


@pytest.mark.parametrize("name", CONTEXTS)
def test_each_service_gets_its_own_database(compose, name):
    url = environment(compose, name)["FOODGO_DATABASE_URL"]
    assert url == f"postgresql://foodgo:foodgo@postgres:5432/{module(name).DATABASE}"  # 服務啟動時自己建


@pytest.mark.parametrize("name", CONTEXTS)
def test_services_find_kafka_and_jaeger_by_name(compose, name):
    env = environment(compose, name)
    assert env["FOODGO_KAFKA_BOOTSTRAP"] == "kafka:9092"  # compose 的 INTERNAL listener
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://jaeger:4318"


def test_the_gateway_finds_the_services_by_name(compose):
    env = environment(compose, "gateway")
    assert {key: value for key, value in env.items() if key.endswith("_URL")} == {
        f"FOODGO_{name.upper()}_URL": f"http://{name}:8000" for name in CONTEXTS
    }
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://jaeger:4318"
    assert "FOODGO_DATABASE_URL" not in env and "FOODGO_KAFKA_BOOTSTRAP" not in env  # Gateway 不碰資料庫與 Kafka


@pytest.mark.parametrize(
    "name, switches",
    [
        ("ordering", {"FOODGO_FAULT_RELAY", "FOODGO_SAGA_TIMEOUT", "FOODGO_SAGA_RETRIES"}),
        ("payment", {"FOODGO_FAULT_PAYMENT", "FOODGO_FAULT_REFUND"}),
        ("kitchen", {"FOODGO_FAULT_KITCHEN", "FOODGO_KITCHEN_SLOW_SECONDS"}),
        ("delivery", set()),
        ("gateway", set()),
    ],
)
def test_fault_switches_only_reach_their_own_service(compose, name, switches):
    env = environment(compose, name)
    assert set(env) & SWITCHES == switches
    for key in switches:
        assert env[key].startswith(f"${{{key}:-"), env[key]  # 從終端機帶進來：FOODGO_FAULT_KITCHEN=crash docker compose …


# ---------------------------------------------------------------- 啟動順序、對外的埠、重啟
@pytest.mark.parametrize("name", CONTEXTS)
def test_services_wait_for_healthy_infrastructure(compose, name):
    assert compose[name]["depends_on"] == {dep: {"condition": "service_healthy"} for dep in ("kafka", "postgres", "jaeger")}


def test_the_gateway_waits_for_the_four_services(compose):
    assert compose["gateway"]["depends_on"] == {name: {"condition": "service_healthy"} for name in CONTEXTS}


def test_only_the_gateway_is_published(compose):
    assert compose["gateway"]["ports"] == ["${FOODGO_GATEWAY_PORT:-8000}:8000"]
    for name in CONTEXTS:
        assert "ports" not in compose[name], name  # 只有容器之間連得到
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "FOODGO_GATEWAY_PORT=8000" in example
    assert "COMPOSE_PROFILES=app" in example


def test_compose_config_is_valid():
    if shutil.which("docker") is None:
        pytest.skip("沒有 docker 指令")
    result = subprocess.run(
        ["docker", "compose", "--profile", "app", "config", "--format", "json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")},  # 不吃終端機的故障開關
    )
    if result.returncode != 0 and "compose" in result.stderr and "unknown" in result.stderr:
        pytest.skip("沒有 docker compose v2")
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert set(services) == {*INFRA, *APPS}
    assert services["kitchen"]["environment"]["FOODGO_FAULT_KITCHEN"] == ""  # 沒設 → 空字串＝沒故障
    assert services["ordering"]["environment"]["FOODGO_SAGA_TIMEOUT"] == "3"


# ---------------------------------------------------------------- 詞彙表
@pytest.mark.parametrize(
    "term",
    [
        "映像檔",
        "容器",
        "Dockerfile",
        "多階段建置",
        ".dockerignore",
        "build context",
        "層",
        "--mount=type=cache",
        "非 root",
        "HEALTHCHECK",
        "unhealthy",
        "restart",
        "profile",
        "service_healthy",
        "foodgo:0.11.0",
    ],
)
def test_glossary_explains_containers(term):
    assert term in (ROOT / "glossary.md").read_text(encoding="utf-8")
