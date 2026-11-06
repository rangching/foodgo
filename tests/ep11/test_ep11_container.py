"""第 11 集：真的跑一個容器——非 root、只有虛擬環境、五個服務都 import 得到、健康檢查會變 healthy。

執行：uv run pytest tests/ep11 -q
要 Docker：daemon 開著，而且先建好映像檔（docker compose --profile app build）；沒有就略過。
不用 Kafka、PostgreSQL：只起一個 Gateway 容器（它的 /healthz 不問後面的服務）。
"""
import json
import time
import uuid

from ep11_files import APPS, docker


def run(image: str, *command: str) -> str:
    result = docker("run", "--rm", image, *command)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_it_runs_as_uid_10001(image):
    assert run(image, "id", "-u") == "10001"


def test_only_the_virtualenv_is_in_app(image):
    assert run(image, "ls", "-A", "/app") == ".venv"  # 沒有 src/、tests/、uv.lock
    assert run(image, "sh", "-c", "command -v uv || echo 沒有 uv") == "沒有 uv"  # uv 留在 builder


def test_the_code_belongs_to_root(image):
    # foodgo 這個使用者只能讀：容器被打穿也改不了程式
    assert run(image, "sh", "-c", "touch /app/.venv/x 2>/dev/null && echo 寫得進去 || echo 唯讀") == "唯讀"


def test_all_five_services_are_in_one_image(image):
    modules = ", ".join(f"foodgo.services.{name}" for name in APPS)
    assert run(image, "python", "-c", f"import {modules}; print('ok')") == "ok"


def test_the_image_metadata(config):
    assert config["User"] == "10001:10001"
    assert config["Cmd"] == ["python", "-m", "foodgo.services.gateway"]
    assert config["Healthcheck"]["Test"] == ["CMD", "python", "-m", "foodgo.infra.healthcheck"]
    assert "8000/tcp" in config["ExposedPorts"]
    assert "FOODGO_HOST=0.0.0.0" in config["Env"]


def test_the_healthcheck_fails_when_nothing_listens(image):
    result = docker("run", "--rm", image, "python", "-m", "foodgo.infra.healthcheck")
    assert result.returncode == 1
    assert "連不上" in result.stdout


def test_a_gateway_container_turns_healthy(image):
    name = f"foodgo-ep11-test-{uuid.uuid4().hex[:8]}"
    started = docker("run", "-d", "--name", name, image)
    assert started.returncode == 0, started.stderr
    try:
        status, began = "", time.monotonic()
        while time.monotonic() - began < 30:
            status = docker("inspect", "--format", "{{.State.Health.Status}}", name).stdout.strip()
            if status in ("healthy", "unhealthy"):
                break
            time.sleep(0.5)
        assert status == "healthy"
        health = json.loads(docker("inspect", "--format", "{{json .State.Health}}", name).stdout)
        assert '"service":"gateway"' in health["Log"][-1]["Output"]  # 健康檢查印的 /healthz 回應
    finally:
        docker("rm", "-f", name)

