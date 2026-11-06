"""第 12 集：真的叢集——全部就緒、非 root、唯讀、Gateway 找得到四個服務、一張單走完、
三個廚房各分到一個 partition、砍掉的 Pod 會被補回來。

執行：uv run pytest tests/ep12 -q
要一個跑著 FoodGo 的 kind 叢集（kind create cluster --config kind-config.yaml、kind load、kubectl apply -k k8s/）；
沒有就略過。叢集名字不是 foodgo 的話設 FOODGO_KIND_CLUSTER。會多一張訂單（A00x）、換掉一個廚房的 Pod。
"""
import json
import time

import pytest
from ep12_k8s import APPS, kubectl

pytestmark = pytest.mark.kind

ORDER = {
    "customer_id": "C042",
    "restaurant_id": "R007",
    "lines": [{"name": "牛肉麵", "unit_price": 160, "quantity": 1}, {"name": "紅茶", "unit_price": 40, "quantity": 1}],
    "delivery_fee": 30,
    "note": "不要香菜",
    "delivery_address": "信義路五段 7 號",
}


def k(*args: str, timeout: float = 120) -> str:
    result = kubectl(*args, timeout=timeout, context=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def get(kind: str, name: str = "") -> dict:
    return json.loads(k("get", kind, *([name] if name else []), "-o", "json"))


def in_gateway(code: str) -> str:
    """在 Gateway 的容器裡跑一段 Python（映像檔裡沒有 curl）：從叢集裡面打 Gateway 自己。"""
    return k("exec", "deploy/gateway", "--", "python", "-c", code)


def test_everything_is_ready():
    for name in (*APPS, "jaeger"):
        status = get("deployment", name)["status"]
        assert status.get("availableReplicas") == status.get("replicas"), name
    for name in ("kafka", "postgres"):
        assert get("statefulset", name)["status"].get("readyReplicas") == 1, name
    for p in get("pods")["items"]:
        if p["metadata"].get("deletionTimestamp"):  # 正在換掉的舊 Pod 不算
            continue
        assert all(c["ready"] for c in p["status"]["containerStatuses"]), p["metadata"]["name"]


def test_the_pods_run_the_built_version():
    from foodgo.infra.service import VERSION

    for name in APPS:
        spec = get("deployment", name)["spec"]["template"]["spec"]
        assert spec["containers"][0]["image"] == f"foodgo:{VERSION}", name


@pytest.mark.parametrize("name", APPS)
def test_containers_run_as_10001_on_a_read_only_filesystem(name):
    assert k("exec", f"deploy/{name}", "--", "id", "-u").strip() == "10001"
    tried = k("exec", f"deploy/{name}", "--", "sh", "-c", "touch /tmp/x 2>/dev/null && echo 寫得進去 || echo 唯讀")
    assert tried.strip() == "唯讀"


def test_the_gateway_finds_the_four_services_by_name():
    out = in_gateway(
        "import json, urllib.request;"
        "print(urllib.request.urlopen('http://127.0.0.1:8000/services', timeout=10).read().decode())"
    )
    answers = json.loads(out)
    assert {name: a["http"] for name, a in answers.items()} == {n: 200 for n in ("ordering", "payment", "kitchen", "delivery")}
    assert answers["kitchen"]["url"] == "http://kitchen:8000"  # ConfigMap 的值，Service 的名字


def test_an_order_goes_all_the_way_through_the_cluster():
    out = in_gateway(
        "import json, time, urllib.request, urllib.error\n"
        f"body = json.dumps({ORDER!r}).encode()\n"
        "request = urllib.request.Request('http://127.0.0.1:8000/orders', body, {'Content-Type': 'application/json'})\n"
        "order = json.load(urllib.request.urlopen(request, timeout=10))\n"
        "status = ''\n"
        "for _ in range(100):\n"
        "    try:\n"
        "        status = json.load(urllib.request.urlopen(f\"http://127.0.0.1:8000/orders/{order['order_id']}/tracking\", timeout=5))['status']\n"
        "    except urllib.error.HTTPError:\n"
        "        status = 'not yet'\n"
        "    if status == 'delivering':\n"
        "        break\n"
        "    time.sleep(0.2)\n"
        "print(json.dumps({'total': order['total'], 'status': status}))\n"
    )
    assert json.loads(out.strip().splitlines()[-1]) == {"total": 230, "status": "delivering"}  # 外送中：付款、接單、派外送員都走完


def kitchen_members() -> dict[str, str]:
    """consumer group kitchen 的成員：CLIENT-ID（＝Pod 的名字）→ 分到的 partition。"""
    out = k(
        "exec", "kafka-0", "--", "/opt/kafka/bin/kafka-consumer-groups.sh",
        "--bootstrap-server", "localhost:9092", "--describe", "--group", "kitchen", "--members", "--verbose",
    )  # fmt: skip
    members = {}
    for line in out.splitlines():  # GROUP CONSUMER-ID HOST CLIENT-ID #PARTITIONS … CURRENT-ASSIGNMENT …
        parts = line.split()
        if parts and parts[0] == "kitchen":
            members[parts[3]] = " ".join(p for p in parts if p.startswith("kitchen.commands"))
    return members


def wait_for(check, seconds: float = 90):
    began, last = time.monotonic(), None
    while time.monotonic() - began < seconds:
        last = check()
        if last:
            return last
        time.sleep(2)
    raise AssertionError(f"等了 {seconds:g} 秒：{last!r}")


def kitchen_pods() -> set[str]:
    return {
        p["metadata"]["name"]
        for p in get("pods")["items"]
        if p["metadata"]["labels"].get("app") == "kitchen" and not p["metadata"].get("deletionTimestamp")
    }


def test_each_kitchen_gets_one_partition():
    pods = kitchen_pods()
    assert len(pods) == 3

    def settled():
        members = kitchen_members()
        return members if set(members) == pods and len(set(members.values())) == 3 else None

    members = wait_for(settled)
    assert sorted(members.values()) == ["kitchen.commands:0", "kitchen.commands:1", "kitchen.commands:2"]


def test_a_deleted_kitchen_pod_is_replaced():
    before = kitchen_pods()
    victim = sorted(before)[0]
    k("delete", "pod", victim, "--wait=true")

    def replaced():
        now = kitchen_pods()
        ready = get("deployment", "kitchen")["status"].get("readyReplicas")
        return now if len(now) == 3 and victim not in now and ready == 3 else None

    after = wait_for(replaced)
    assert len(after - before) == 1  # ReplicaSet 補了一個新的，名字不一樣
