"""第 4 集：compose.yaml——Kafka（KRaft）、Kafka UI、PostgreSQL，版本釘死、有健康檢查、埠號不撞。

執行：uv run pytest tests/ep04 -q（這個檔不需要 Docker）
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def services() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]


def test_three_services(services):
    # 第 10 集起多了 jaeger（tests/ep10 檢查它）：這裡只要求第 4 集的三個都還在
    assert {"kafka", "kafka-ui", "postgres"} <= set(services)
    assert services["kafka"]["image"].startswith("apache/kafka:")
    assert services["kafka-ui"]["image"].startswith("kafbat/kafka-ui:")
    assert services["postgres"]["image"].startswith("postgres:17.")


def test_images_are_pinned(services):
    for name, service in services.items():
        tag = service["image"].rsplit(":", 1)[1]
        assert tag != "latest" and re.fullmatch(r"v?\d+\.\d+(\.\d+)?", tag), f"{name} 要釘到明確版本：{tag}"


def test_every_service_has_a_healthcheck(services):
    for name, service in services.items():
        if "build" in service:  # 第 11 集起 FoodGo 自己的容器：健康檢查寫在 Dockerfile 的 HEALTHCHECK（tests/ep11 檢查）
            continue
        assert service["healthcheck"]["test"], f"{name} 沒有健康檢查"


def test_kafka_runs_in_kraft_mode_without_zookeeper(services):
    env = services["kafka"]["environment"]
    assert env["KAFKA_PROCESS_ROLES"] == "broker,controller"
    assert "zookeeper" not in str(services).lower()
    assert env["KAFKA_AUTO_CREATE_TOPICS_ENABLE"] == "false"  # topic 要先建好


def test_kafka_ui_talks_to_kafka_inside_the_network(services):
    assert services["kafka-ui"]["environment"]["KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS"] == "kafka:9092"
    assert services["kafka-ui"]["depends_on"]["kafka"]["condition"] == "service_healthy"


@pytest.mark.parametrize(
    "service, variable, default",
    [
        ("postgres", "FOODGO_POSTGRES_PORT", 15432),
        ("kafka", "FOODGO_KAFKA_PORT", 19092),
        ("kafka-ui", "FOODGO_KAFKA_UI_PORT", 18080),
    ],
)
def test_ports_avoid_the_usual_ones_and_can_be_changed(services, service, variable, default):
    [port] = services[service]["ports"]
    assert port.startswith(f"${{{variable}:-{default}}}:")
    assert variable in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_python_defaults_match_compose(monkeypatch):
    from foodgo.infra import config

    for variable in ["FOODGO_DATABASE_URL", "FOODGO_KAFKA_BOOTSTRAP", "FOODGO_POSTGRES_PORT", "FOODGO_KAFKA_PORT"]:
        monkeypatch.delenv(variable, raising=False)
    assert config.database_url() == "postgresql://foodgo:foodgo@127.0.0.1:15432/foodgo"
    assert config.kafka_bootstrap() == "127.0.0.1:19092"

    monkeypatch.setenv("FOODGO_POSTGRES_PORT", "25432")
    monkeypatch.setenv("FOODGO_KAFKA_PORT", "29092")
    assert config.database_url().endswith(":25432/foodgo")
    assert config.kafka_bootstrap() == "127.0.0.1:29092"
