"""第 11 集：映像檔怎麼做——Dockerfile（多階段、uv 快取、非 root、HEALTHCHECK）、.dockerignore、健康檢查、topic 等到有 leader。

執行：uv run pytest tests/ep11 -q（這個檔不用 Docker：只讀 Dockerfile、在本機跑健康檢查）
"""
import ast
import http.server
import json
import re
import threading
import tomllib
from types import SimpleNamespace

import pytest
from ep11_files import ROOT, instructions, stages

BASE = r"python:3\.12\.\d+-slim-[a-z]+"  # 釘到 patch 版本與 Debian 版本，不用 latest、不用 3.12


# ---------------------------------------------------------------- Dockerfile：兩個階段
def test_two_stages_on_the_same_pinned_base(dockerfile):
    builder, runtime = stages(dockerfile)
    base, _, alias = builder[0][1].partition(" AS ")
    assert re.fullmatch(BASE, base), f"base image 要釘到明確版本：{base}"
    assert alias == "builder"
    # 同一個 base：.venv 裡的 python 指向 /usr/local/bin/python3.12，兩邊要是同一個
    assert runtime[0] == ("FROM", base)


def test_uv_comes_from_its_own_pinned_image(dockerfile):
    builder, _ = stages(dockerfile)
    copies = [rest for keyword, rest in builder if keyword == "COPY" and "astral-sh/uv" in rest]
    assert len(copies) == 1
    match = re.fullmatch(r"--from=ghcr\.io/astral-sh/uv:(\d+\.\d+\.\d+) /uv /bin/uv", copies[0])
    assert match, copies[0]
    # 和 pyproject.toml 的 uv_build 同一個系列（uv_build>=0.12.19,<0.13.0 → uv 0.12.x）
    [requires] = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]["requires"]
    minor = match[1].rsplit(".", 1)[0]
    assert f">={minor}." in requires, (match[1], requires)


def test_dependencies_are_installed_before_the_code(dockerfile):
    builder, _ = stages(dockerfile)
    steps = [(keyword, rest) for keyword, rest in builder if keyword in ("RUN", "COPY")]
    syncs = [i for i, (keyword, rest) in enumerate(steps) if keyword == "RUN" and "uv sync" in rest]
    code = [i for i, (keyword, rest) in enumerate(steps) if keyword == "COPY" and rest.startswith(". ")]
    assert len(syncs) == 2 and len(code) == 1
    first, second = (steps[i][1] for i in syncs)
    assert syncs[0] < code[0] < syncs[1]  # 先裝依賴、再放程式碼：改程式碼不會讓依賴那一層重做
    assert "--no-install-project" in first and "--no-install-project" not in second
    assert "source=uv.lock" in first and "source=pyproject.toml" in first  # 第一次只看得到這兩個檔
    for sync in (first, second):
        assert "--mount=type=cache,target=/root/.cache/uv" in sync  # uv 的快取留在建置機器，不進映像檔
        for flag in ("--locked", "--no-dev", "--no-editable"):
            assert flag in sync, (flag, sync)


def test_the_final_image_takes_only_the_virtualenv(dockerfile):
    _, runtime = stages(dockerfile)
    copies = [rest for keyword, rest in runtime if keyword == "COPY"]
    assert copies == ["--from=builder /app/.venv /app/.venv"]  # 沒有 uv、沒有快取、沒有 src/、沒有測試
    assert not any("uv " in rest or rest.endswith(" uv") for keyword, rest in runtime if keyword == "RUN")


def test_it_runs_as_a_non_root_user(dockerfile):
    _, runtime = stages(dockerfile)
    [user] = [rest for keyword, rest in runtime if keyword == "USER"]
    uid, _, gid = user.partition(":")
    assert uid.isdigit() and int(uid) > 0 and gid.isdigit()  # 用數字：第 12 集 runAsNonRoot 要看得出不是 0
    [created] = [rest for keyword, rest in runtime if keyword == "RUN" and "useradd" in rest]
    assert f"--uid {uid}" in created and f"--gid {gid}" in created
    assert [keyword for keyword, _ in runtime].index("USER") > [keyword for keyword, _ in runtime].index("COPY")


def test_every_service_listens_on_8000_inside_the_container(dockerfile):
    from foodgo.infra import config

    _, runtime = stages(dockerfile)
    env = " ".join(rest for keyword, rest in runtime if keyword == "ENV")
    assert "FOODGO_HOST=0.0.0.0" in env  # 127.0.0.1 只有容器自己連得到
    for name in config.PORTS:
        assert f"FOODGO_{name.upper()}_PORT=8000" in env
    assert 'PATH="/app/.venv/bin:$PATH"' in env
    assert ("EXPOSE", "8000") in runtime


def test_the_healthcheck_asks_healthz(dockerfile):
    _, runtime = stages(dockerfile)
    [check] = [rest for keyword, rest in runtime if keyword == "HEALTHCHECK"]
    options, _, command = check.partition(" CMD ")
    assert json.loads(command) == ["python", "-m", "foodgo.infra.healthcheck"]
    for option in ("--interval=", "--timeout=", "--start-period=", "--retries="):
        assert option in options


def test_cmd_is_in_exec_form(dockerfile):
    _, runtime = stages(dockerfile)
    [cmd] = [rest for keyword, rest in runtime if keyword == "CMD"]
    assert json.loads(cmd) == ["python", "-m", "foodgo.services.gateway"]  # python 是 PID 1，收得到 SIGTERM


def test_no_floating_tags_anywhere(dockerfile):
    for keyword, rest in instructions(dockerfile):
        for ref in re.findall(r"(?:FROM |--from=)(\S+)", f"{keyword} {rest}"):
            if ref != "builder":
                assert re.search(r":\d+\.\d+\.\d+", ref) and not ref.endswith(":latest"), ref


# ---------------------------------------------------------------- .dockerignore
def ignored(rules: list[str], path: str) -> bool:
    """照 Docker 的規則判斷：最後一條符合的說了算；! 開頭是放回來；資料夾被排除，裡面的也跟著。"""

    def regex(pattern: str) -> str:
        out = ""
        for part in re.split(r"(\*\*/|\*\*|\*|\?)", pattern.strip("/")):
            out += {"**/": "(.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(part, re.escape(part))
        return out

    def matches(pattern: str) -> bool:
        parts = path.split("/")
        return any(re.fullmatch(regex(pattern), "/".join(parts[:n])) for n in range(1, len(parts) + 1))

    result = False
    for rule in rules:
        negate = rule.startswith("!")
        if matches(rule.lstrip("!")):
            result = not negate
    return result


@pytest.mark.parametrize(
    "path",
    ["pyproject.toml", "uv.lock", ".python-version", "README.md", "src/foodgo/app.py", "src/foodgo/infra/healthcheck.py"],
)
def test_the_build_context_has_what_the_image_needs(path):
    rules = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()]
    rules = [line for line in rules if line and not line.startswith("#")]
    assert not ignored(rules, path)


@pytest.mark.parametrize(
    "path",
    [
        ".env",  # 密碼、埠號：不能進映像檔
        ".venv/bin/python",  # 本機的虛擬環境（macOS 的，容器裡也不能用）
        ".git/HEAD",
        "tests/ep11/test_ep11_image.py",
        "demos/ep10_services.py",
        "compose.yaml",
        "Dockerfile",
        "glossary.md",
        "src/foodgo/__pycache__/app.cpython-312.pyc",
        ".pytest_cache/README.md",
        "some_new_file.txt",  # 新增的檔案預設不送
    ],
)
def test_the_build_context_leaves_out_the_rest(path):
    rules = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()]
    rules = [line for line in rules if line and not line.startswith("#")]
    assert ignored(rules, path)


# ---------------------------------------------------------------- 健康檢查：python -m foodgo.infra.healthcheck
@pytest.fixture
def server():
    """一個假的服務：/healthz 回 200，/broken 回 503。"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status = 200 if self.path == "/healthz" else 503
            body = json.dumps({"service": "kitchen", "status": "ok" if status == 200 else "failing"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_healthy_exits_with_0(server, capsys):
    from foodgo.infra import healthcheck

    assert healthcheck.main([f"{server}/healthz"]) == 0
    assert '"status": "ok"' in capsys.readouterr().out  # 印出來的會留在 docker inspect 的 Health.Log


def test_503_exits_with_1(server, capsys):
    from foodgo.infra import healthcheck

    assert healthcheck.main([f"{server}/broken"]) == 1
    assert capsys.readouterr().out.startswith("503 ")


def test_nothing_listening_exits_with_1(capsys):
    from foodgo.infra import healthcheck

    assert healthcheck.main(["http://127.0.0.1:1/healthz"]) == 1
    assert "連不上" in capsys.readouterr().out


def test_the_default_is_port_8000():
    from foodgo.infra import healthcheck

    assert healthcheck.URL == "http://127.0.0.1:8000/healthz"


def test_the_healthcheck_uses_only_the_standard_library():
    tree = ast.parse((ROOT / "src" / "foodgo" / "infra" / "healthcheck.py").read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert names <= {"sys", "urllib.request", "urllib.error"}  # 每 10 秒跑一次：不載入 FastAPI、Kafka


# ---------------------------------------------------------------- 容器一起起來才撞到的：topic 要等到有 leader
class FakeAdmin:
    """假的 Kafka AdminClient：topic 已經列得出來，前幾次問 leader 還沒選好。"""

    def __init__(self, ready_after: int):
        self.asked, self.ready_after, self.created = 0, ready_after, []

    def list_topics(self, topic=None, timeout=None):
        if topic is None:
            return SimpleNamespace(topics={"kitchen.events": None})  # 廚房服務剛建好
        self.asked += 1
        leader = 1 if self.asked >= self.ready_after else -1
        partitions = {n: SimpleNamespace(leader=leader, error=None) for n in range(3)}
        return SimpleNamespace(topics={topic: SimpleNamespace(error=None, partitions=partitions)})

    def create_topics(self, topics):
        self.created += [t.topic for t in topics]
        return {t.topic: SimpleNamespace(result=lambda: None) for t in topics}


def test_ensure_topic_waits_until_every_partition_has_a_leader(monkeypatch):
    from foodgo.infra import kafka

    admin = FakeAdmin(ready_after=3)
    monkeypatch.setattr(kafka, "AdminClient", lambda conf: admin)
    kafka.ensure_topic("kitchen.events")
    assert admin.created == []  # 別人建好了，不再建
    assert admin.asked == 3  # 問到第三次才有 leader


def test_ensure_topic_gives_up_after_the_timeout(monkeypatch):
    from foodgo.infra import kafka

    monkeypatch.setattr(kafka, "AdminClient", lambda conf: FakeAdmin(ready_after=10**9))
    with pytest.raises(TimeoutError, match="kitchen.events"):
        kafka.ensure_topic("kitchen.events", timeout=0.3)
