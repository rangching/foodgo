"""第 11 集測試共用的小工具：讀 Dockerfile、叫 docker 指令。測試檔用 from ep11_files import …"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPS = ("gateway", "ordering", "payment", "kitchen", "delivery")  # 五個 FoodGo 容器
INFRA = ("kafka", "kafka-ui", "postgres", "jaeger")  # 第 4、10 集的基礎設施


def instructions(text: str) -> list[tuple[str, str]]:
    """Dockerfile → [(指令, 參數)]：去掉註解、接起行尾的反斜線。"""
    lines, current = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if line.startswith("#"):  # 接續行中間的註解
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        keyword, _, rest = current.partition(" ")
        lines.append((keyword.upper(), re.sub(r"\s+", " ", rest).strip()))
        current = ""
    return lines


def stages(text: str) -> list[list[tuple[str, str]]]:
    """照 FROM 切成一段一段（多階段建置）。"""
    result: list[list[tuple[str, str]]] = []
    for keyword, rest in instructions(text):
        if keyword == "FROM":
            result.append([])
        result[-1].append((keyword, rest))
    return result


def docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
