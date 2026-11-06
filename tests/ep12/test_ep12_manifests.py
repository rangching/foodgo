"""第 12 集：k8s/（kustomize）與 kind-config.yaml——不用叢集，只要 kubectl 指令（kubectl kustomize 只組 YAML）。

執行：uv run pytest tests/ep12 -q
檢查：組得出來、全部在 foodgo namespace、五個 Deployment 一個映像檔、非 root、資源上下限、三種探針、
Service 的名字對得上 ConfigMap（和第 11 集 compose 的環境變數一模一樣）、密碼在 Secret、映像檔版本只寫一個地方、
基礎設施的映像檔和 compose 同一版、kind 的節點映像檔釘死、對外的埠對得上、詞彙表。
"""
import importlib
import re
import threading
from urllib.parse import urlsplit

import pytest
import yaml
from ep12_k8s import APPS, CONTEXTS, ROOT, container, environment, find, quantity, render, secret

WORKLOADS = ("Deployment", "StatefulSet")


def module(name: str):
    return importlib.import_module(f"foodgo.services.{name}")


def pod(manifests, name: str, kind: str = "Deployment") -> dict:
    return find(manifests, kind, name)["spec"]["template"]["spec"]


def compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]


# ---------------------------------------------------------------- kustomize 組得出來
def test_everything_lives_in_the_foodgo_namespace(manifests):
    kinds = {o["kind"] for o in manifests}
    assert {"Namespace", "ConfigMap", "Secret", "Service", "Deployment", "StatefulSet", "PersistentVolumeClaim"} <= kinds
    assert find(manifests, "Namespace", "foodgo")
    for o in manifests:
        if o["kind"] != "Namespace":
            assert o["metadata"]["namespace"] == "foodgo", (o["kind"], o["metadata"]["name"])


def test_five_deployments_one_image(manifests):
    from foodgo.infra.service import VERSION

    deployments = {o["metadata"]["name"] for o in manifests if o["kind"] == "Deployment"}
    assert deployments == {*APPS, "jaeger"}
    for name in APPS:
        c = container(manifests, "Deployment", name)
        assert c["image"] == f"foodgo:{VERSION}"  # 一個映像檔（第 11 集）
        assert c["command"] == ["python", "-m", f"foodgo.services.{name}"]  # 只差啟動哪一個
        assert c["imagePullPolicy"] == "IfNotPresent"  # 用 kind load 載進來的，不去 registry 拉


def test_the_version_is_written_in_one_place(kustomization):
    from foodgo.infra.service import VERSION

    assert kustomization["images"] == [{"name": "foodgo", "newTag": VERSION}]
    for name in APPS:
        text = (ROOT / "k8s" / "apps" / f"{name}.yaml").read_text(encoding="utf-8")
        assert re.findall(r"^\s+image: (\S+)", text, re.MULTILINE) == ["foodgo"], name  # tag 由 kustomization 決定
    assert compose()["kitchen"]["image"] == f"foodgo:{VERSION}"  # compose 也跟著（第 11 集的規則）


def test_the_version_is_0_12_0():
    from foodgo.infra.service import VERSION

    assert VERSION == "0.12.0"  # 滾動更新：0.11.0 → 0.12.0


# ---------------------------------------------------------------- 每個 FoodGo 容器
@pytest.mark.parametrize("name", APPS)
def test_it_runs_as_non_root(manifests, name):
    security = pod(manifests, name)["securityContext"]
    assert security["runAsNonRoot"] is True  # 是 root 就不准起
    assert security["runAsUser"] == 10001 and security["runAsGroup"] == 10001  # 和映像檔的 USER 一樣（第 11 集）
    c = container(manifests, "Deployment", name)["securityContext"]
    assert c["allowPrivilegeEscalation"] is False
    assert c["readOnlyRootFilesystem"] is True
    assert c["capabilities"]["drop"] == ["ALL"]


@pytest.mark.parametrize("name", APPS)
def test_it_asks_for_and_is_limited_in_resources(manifests, name):
    resources = container(manifests, "Deployment", name)["resources"]
    for what in ("cpu", "memory"):
        request, limit = quantity(resources["requests"][what]), quantity(resources["limits"][what])
        assert 0 < request <= limit, (what, request, limit)
    assert quantity(resources["limits"]["memory"]) >= 200 * 2**20  # 實測每個約 55～60 MiB，留空間


@pytest.mark.parametrize("name", APPS)
def test_it_has_three_probes(manifests, name):
    c = container(manifests, "Deployment", name)
    assert c["ports"] == [{"name": "http", "containerPort": 8000}]  # 映像檔裡五個都聽 8000（第 11 集）
    ready = "/healthz" if name == "gateway" else "/readyz"  # Gateway 沒有 /readyz
    for probe, path in (("startupProbe", "/healthz"), ("livenessProbe", "/healthz"), ("readinessProbe", ready)):
        assert c[probe]["httpGet"] == {"path": path, "port": "http"}, probe
    startup = c["startupProbe"]
    assert startup["periodSeconds"] * startup["failureThreshold"] >= 30  # 建資料庫、建表、建 topic 要時間
    liveness = c["livenessProbe"]
    assert liveness["periodSeconds"] * liveness["failureThreshold"] >= 20  # 不要一次失敗就砍


def test_services_do_not_get_service_links(manifests):
    # 名叫 kafka 的 Service 會讓 K8S 塞 KAFKA_PORT=tcp://…，Kafka 的映像檔會把它當設定
    for o in manifests:
        if o["kind"] in WORKLOADS:
            assert o["spec"]["template"]["spec"]["enableServiceLinks"] is False, o["metadata"]["name"]


# ---------------------------------------------------------------- 設定：和第 11 集的 compose 一模一樣
def resolved(value: str) -> str:
    """compose 的 ${名字:-預設} → 預設值（沒從終端機帶進來時的樣子）。"""
    return re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", str(value))


@pytest.mark.parametrize("name", APPS)
def test_the_environment_is_the_same_as_compose(manifests, name):
    want = {key: resolved(value) for key, value in compose()[name]["environment"].items()}
    got = environment(manifests, name)
    for key, value in want.items():
        assert got.get(key, "") == value, key  # 故障開關沒寫＝空字串＝沒故障，和 compose 的預設一樣


@pytest.mark.parametrize("name", APPS)
def test_fault_switches_are_not_baked_in(manifests, name):
    names = {item["name"] for item in container(manifests, "Deployment", name).get("env", [])}
    assert not {n for n in names if n.startswith("FOODGO_FAULT_")}  # 要的時候 kubectl set env，不要寫死在檔案裡


def services(manifests) -> dict[str, set[int]]:
    return {o["metadata"]["name"]: {p["port"] for p in o["spec"]["ports"]} for o in manifests if o["kind"] == "Service"}


def test_service_names_match_the_config(manifests):
    env = find(manifests, "ConfigMap", "foodgo")["data"]
    known = services(manifests)
    wanted = [env["FOODGO_KAFKA_BOOTSTRAP"]]
    wanted += [urlsplit(env[key]).netloc for key in env if key.endswith("_URL") or key == "OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert len(wanted) == 6
    for where in wanted:
        host, port = where.rsplit(":", 1)
        assert host in known and int(port) in known[host], where  # 叢集的 DNS 用 Service 的名字找


def test_every_service_reaches_a_container_port(manifests):
    for o in manifests:
        if o["kind"] != "Service":
            continue
        workload = next(
            w for w in manifests if w["kind"] in WORKLOADS and w["spec"]["selector"]["matchLabels"] == o["spec"]["selector"]
        )
        ports = {p["name"] for p in workload["spec"]["template"]["spec"]["containers"][0]["ports"]}
        for port in o["spec"]["ports"]:
            assert port["targetPort"] in ports, (o["metadata"]["name"], port)


@pytest.mark.parametrize("name", CONTEXTS)
def test_each_service_gets_its_database_url_from_the_secret(manifests, name):
    env = container(manifests, "Deployment", name)["env"]
    url = next(item for item in env if item["name"] == "FOODGO_DATABASE_URL")
    assert url["valueFrom"]["secretKeyRef"] == {"name": "foodgo-db", "key": name}  # 整條 URL（含密碼）在 Secret
    plain = secret(manifests)
    assert plain[name] == f"postgresql://foodgo:{plain['POSTGRES_PASSWORD']}@postgres:5432/{module(name).DATABASE}"


def test_no_password_outside_the_secret(manifests):
    assert "FOODGO_DATABASE_URL" not in environment(manifests, "gateway")  # Gateway 不碰資料庫
    for o in manifests:
        if o["kind"] == "ConfigMap":
            assert "foodgo:foodgo@" not in yaml.safe_dump(o)
    env = container(manifests, "StatefulSet", "postgres")["env"]
    password = next(item for item in env if item["name"] == "POSTGRES_PASSWORD")
    assert password["valueFrom"]["secretKeyRef"] == {"name": "foodgo-db", "key": "POSTGRES_PASSWORD"}


# ---------------------------------------------------------------- 基礎設施：教學用的最簡版本
def test_infrastructure_uses_the_same_images_as_compose(manifests):
    for kind, name in (("StatefulSet", "kafka"), ("StatefulSet", "postgres"), ("Deployment", "jaeger")):
        assert container(manifests, kind, name)["image"] == compose()[name]["image"], name


def test_no_floating_tags(manifests):
    for o in (*manifests, *render("k8s/kafka-ui")):
        if o["kind"] in WORKLOADS:
            for c in o["spec"]["template"]["spec"]["containers"]:
                assert re.search(r":v?\d+\.\d+(\.\d+)?$", c["image"]), c["image"]  # postgres 只有 17.11 兩段


def test_kafka_and_postgres_keep_their_data(manifests):
    for name in ("kafka", "postgres"):
        spec = pod(manifests, name, "StatefulSet")  # 名字固定（kafka-0）、同一時間最多一個
        claims = [v["persistentVolumeClaim"]["claimName"] for v in spec["volumes"]]
        assert claims == [f"{name}-data"]
        assert find(manifests, "PersistentVolumeClaim", f"{name}-data")


def test_kafka_advertises_its_service_name(manifests):
    env = {item["name"]: item.get("value") for item in container(manifests, "StatefulSet", "kafka")["env"]}
    assert env["KAFKA_ADVERTISED_LISTENERS"] == "INTERNAL://kafka:9092"  # 服務用 kafka:9092 找它
    assert env["KAFKA_AUTO_CREATE_TOPICS_ENABLE"] == "false"  # topic 由服務自己建（第 4 集）


def test_three_kitchens_for_three_partitions(manifests):
    assert find(manifests, "Deployment", "kitchen")["spec"]["replicas"] == 3  # kitchen.commands 3 個 partition
    for name in ("gateway", "ordering", "payment", "delivery"):
        assert find(manifests, "Deployment", name)["spec"]["replicas"] == 1


def test_consumers_tell_kafka_which_pod_they_are(monkeypatch):
    from foodgo.infra import kafka

    seen = {}

    class FakeConsumer:
        def __init__(self, settings):
            seen.update(settings)

        def subscribe(self, topics, on_assign):
            pass

        def close(self):
            pass

    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(kafka, "Consumer", FakeConsumer)
    monkeypatch.setattr(kafka.socket, "gethostname", lambda: "kitchen-7d4f9c8b6-x2kqp")
    kafka.consume("kitchen.commands", "kitchen", lambda message: None, stop=stop)
    assert seen["client.id"] == "kitchen-7d4f9c8b6-x2kqp"  # K8S 裡主機名稱＝Pod 的名字


# ---------------------------------------------------------------- 從叢集外連進來：kind-config.yaml
def test_the_kind_node_image_is_pinned(kind_config):
    assert kind_config["name"] == "foodgo"
    nodes = kind_config["nodes"]
    assert [n["role"] for n in nodes] == ["control-plane"]  # 一個節點：省記憶體
    assert re.fullmatch(r"kindest/node:v1\.\d+\.\d+@sha256:[0-9a-f]{64}", nodes[0]["image"])


def node_ports(objects) -> dict[str, int]:
    return {
        o["metadata"]["name"]: p["nodePort"]
        for o in objects
        if o["kind"] == "Service" and o["spec"].get("type") == "NodePort"
        for p in o["spec"]["ports"]
    }


def test_only_the_gateway_and_the_uis_are_reachable_from_outside(manifests, kind_config):
    ports = node_ports(manifests)
    assert ports == {"gateway": 30080, "jaeger-ui": 30686}
    ports |= node_ports(render("k8s/kafka-ui"))
    mapped = {m["containerPort"]: m for m in kind_config["nodes"][0]["extraPortMappings"]}
    assert set(mapped) == set(ports.values())
    assert mapped[30080]["hostPort"] == 8000  # http://127.0.0.1:8000 和第 10、11 集一樣
    for m in mapped.values():
        assert m["listenAddress"] == "127.0.0.1"  # 只有自己的電腦連得到


def test_kafka_ui_is_optional(manifests):
    assert all(o["metadata"]["name"] != "kafka-ui" for o in manifests)  # apply -k k8s/ 不裝它
    ui = render("k8s/kafka-ui")
    assert container(ui, "Deployment", "kafka-ui")["image"] == compose()["kafka-ui"]["image"]
    assert all(o["metadata"]["namespace"] == "foodgo" for o in ui)


# ---------------------------------------------------------------- 詞彙表
@pytest.mark.parametrize(
    "term",
    [
        "kind",
        "kubectl",
        "節點",
        "控制平面",
        "Pod",
        "Deployment",
        "ReplicaSet",
        "StatefulSet",
        "Service",
        "NodePort",
        "ConfigMap",
        "Secret",
        "namespace",
        "kustomize",
        "存活探針",
        "就緒探針",
        "啟動探針",
        "CrashLoopBackOff",
        "滾動更新",
        "rollout undo",
        "requests",
        "limits",
        "宣告式",
        "foodgo:0.12.0",
    ],
)
def test_glossary_explains_kubernetes(term):
    assert term in (ROOT / "glossary.md").read_text(encoding="utf-8")
