"""第 12 集的 fixture：組好的 k8s/ YAML、kind-config.yaml；要真的叢集的測試用 cluster。

test_ep12_manifests.py 只要有 kubectl 指令（kubectl kustomize 不碰叢集）。
test_ep12_cluster.py 標了 @pytest.mark.kind：要一個跑著 FoodGo 的 kind 叢集——
    kind create cluster --config kind-config.yaml
    kind load docker-image foodgo:<版本> --name foodgo
    kubectl apply -k k8s/
沒有就自動略過（skipped）。叢集名字不是 foodgo 的話設 FOODGO_KIND_CLUSTER。
"""
import subprocess

import pytest
import yaml
from ep12_k8s import ROOT, cluster_name, have_kubectl, kubectl, render

REASON = "沒有跑著 FoodGo 的 kind 叢集：kind create cluster --config kind-config.yaml、kubectl apply -k k8s/（叢集測試才需要）"


def pytest_configure(config):
    config.addinivalue_line("markers", "kind: 需要一個跑著 FoodGo 的 kind 叢集（kubectl apply -k k8s/）")


def cluster_up() -> bool:
    """kind-<名字> 這個 context 連得上，而且已經有 foodgo namespace。"""
    if not have_kubectl():
        return False
    try:
        found = kubectl("get", "namespace", "foodgo", "--request-timeout=5s", timeout=15, context=True)
    except subprocess.TimeoutExpired:
        return False
    return found.returncode == 0


def pytest_collection_modifyitems(config, items):
    clustered = [item for item in items if item.get_closest_marker("kind")]  # 不用 keywords：參數叫 kind 的也會算進去
    if clustered and not cluster_up():
        for item in clustered:
            item.add_marker(pytest.mark.skip(reason=REASON))


@pytest.fixture(scope="session")
def manifests() -> tuple[dict, ...]:
    """kubectl kustomize k8s/ 組出來的全部物件。"""
    if not have_kubectl():
        pytest.skip("沒有 kubectl 指令：brew install kubectl")
    return render("k8s")


@pytest.fixture(scope="session")
def kind_config() -> dict:
    return yaml.safe_load((ROOT / "kind-config.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def kustomization() -> dict:
    return yaml.safe_load((ROOT / "k8s" / "kustomization.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def cluster() -> str:
    """kind 叢集的名字（給叢集測試用；pytest_collection_modifyitems 已經確定連得上）。"""
    return cluster_name()
