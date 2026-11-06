"""第 11 集的 fixture：讀 Dockerfile、compose.yaml；要真的跑容器的測試用 image。

test_ep11_container.py 要 Docker（daemon 開著、映像檔建好了）：
    docker compose --profile app build      # 或 docker build -t foodgo:0.11.0 .
沒有就自動略過（skipped），其他檔照跑。
"""
import json
import shutil
import subprocess

import pytest
import yaml
from ep11_files import ROOT, docker


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]


# ---------------------------------------------------------------- 要 Docker 的
@pytest.fixture(scope="session")
def image() -> str:
    """建好的 foodgo 映像檔名稱（foodgo:<版本>）；沒有 docker、daemon 沒開、還沒建，就略過。"""
    from foodgo.infra.service import VERSION

    name = f"foodgo:{VERSION}"
    if shutil.which("docker") is None:
        pytest.skip("沒有 docker 指令")
    try:
        found = docker("image", "inspect", name, timeout=15)
    except subprocess.TimeoutExpired:
        pytest.skip("Docker 沒有回應")
    if found.returncode != 0:
        pytest.skip(f"還沒有 {name}：先 docker compose --profile app build（Docker 要開著）")
    return name


@pytest.fixture(scope="session")
def config(image) -> dict:
    """docker image inspect 的 Config：USER、ENV、HEALTHCHECK、CMD…"""
    return json.loads(docker("image", "inspect", image).stdout)[0]["Config"]
