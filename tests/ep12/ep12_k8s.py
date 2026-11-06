"""第 12 集測試共用的小工具：組 k8s/ 的 YAML、叫 kubectl。測試檔用 from ep12_k8s import …"""
import base64
import os
import shutil
import subprocess
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
APPS = ("gateway", "ordering", "payment", "kitchen", "delivery")  # 五個 FoodGo 的 Deployment
CONTEXTS = ("ordering", "payment", "kitchen", "delivery")  # 有自己資料庫的四個服務
NAMESPACE = "foodgo"


def cluster_name() -> str:
    """kind 叢集的名字：預設 foodgo（kind-config.yaml）；換了名字就設 FOODGO_KIND_CLUSTER。"""
    return os.environ.get("FOODGO_KIND_CLUSTER", "foodgo")


def kubectl(*args: str, timeout: float = 60, context: bool = False) -> subprocess.CompletedProcess:
    """跑一次 kubectl。context=True：對 kind 叢集（kind-<名字>）的 foodgo namespace。"""
    where = ["--context", f"kind-{cluster_name()}", "--namespace", NAMESPACE] if context else []
    return subprocess.run(
        ["kubectl", *where, *args], capture_output=True, text=True, timeout=timeout, check=False, cwd=ROOT
    )


def have_kubectl() -> bool:
    return shutil.which("kubectl") is not None


@cache
def render(path: str = "k8s") -> tuple[dict, ...]:
    """kubectl kustomize <path>：組好的全部物件（不碰叢集）。"""
    result = kubectl("kustomize", path)
    assert result.returncode == 0, result.stderr
    return tuple(doc for doc in yaml.safe_load_all(result.stdout) if doc)


def find(objects: tuple[dict, ...], kind: str, name: str) -> dict:
    hits = [o for o in objects if o["kind"] == kind and o["metadata"]["name"] == name]
    assert len(hits) == 1, f"要剛好一個 {kind}/{name}，找到 {len(hits)} 個"
    return hits[0]


def container(objects: tuple[dict, ...], kind: str, name: str) -> dict:
    """某個 Deployment／StatefulSet 唯一的那個容器。"""
    containers = find(objects, kind, name)["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1, name
    return containers[0]


def config(objects: tuple[dict, ...]) -> dict[str, str]:
    """ConfigMap foodgo 的內容。"""
    return find(objects, "ConfigMap", "foodgo")["data"]


def secret(objects: tuple[dict, ...]) -> dict[str, str]:
    """Secret foodgo-db 解開 base64 之後的內容。"""
    found = find(objects, "Secret", "foodgo-db")
    plain = {k: base64.b64decode(v).decode() for k, v in found.get("data", {}).items()}
    return plain | found.get("stringData", {})


def environment(objects: tuple[dict, ...], name: str) -> dict[str, str]:
    """一個 FoodGo 容器拿到的環境變數：ConfigMap（envFrom）＋env（Secret 的照 key 換成值）。"""
    c = container(objects, "Deployment", name)
    env: dict[str, str] = {}
    for source in c.get("envFrom", []):
        assert source["configMapRef"]["name"] == "foodgo"
        env |= config(objects)
    for item in c.get("env", []):
        if "valueFrom" in item:
            ref = item["valueFrom"]["secretKeyRef"]
            assert ref["name"] == "foodgo-db", item
            env[item["name"]] = secret(objects)[ref["key"]]
        else:
            env[item["name"]] = item.get("value", "")
    return env


QUANTITY = {"m": 1e-3, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "": 1}


def quantity(text: str | int) -> float:
    """K8S 的數量（500m、256Mi、1）→ 數字。"""
    text = str(text)
    for suffix in ("Ki", "Mi", "Gi", "m"):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * QUANTITY[suffix]
    return float(text)
