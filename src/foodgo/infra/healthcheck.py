"""容器的健康檢查（第 11 集）：Dockerfile 的 HEALTHCHECK 每 10 秒在容器裡跑一次這個。

    python -m foodgo.infra.healthcheck                              # 問 http://127.0.0.1:8000/healthz
    python -m foodgo.infra.healthcheck http://127.0.0.1:8103/healthz

200 → 結束碼 0（健康）；其他狀態碼、連不上、逾時 → 結束碼 1（不健康）。Docker 只看結束碼。
回應的內容印出來：docker inspect 的 .State.Health.Log 會留著最近幾次的輸出，看得到是哪個背景工作停了。

只用標準函式庫：映像檔裡沒有 curl／wget，而且每 10 秒跑一次，不要載入 FastAPI、Kafka 那些大套件。
"""
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8000/healthz"  # 映像檔裡五個程序都聽 8000（Dockerfile 的 ENV）


def check(url: str = URL, *, timeout: float = 2.0) -> tuple[bool, str]:
    """問一次 /healthz：(健康嗎, 說明)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200, f"{response.status} {response.read().decode()}"
    except urllib.error.HTTPError as error:  # 503：有背景工作停了
        return False, f"{error.code} {error.read().decode()}"
    except (OSError, ValueError) as error:  # 連不上（還沒起來、已經掛了）、逾時
        return False, f"連不上 {url}：{error}"


def main(argv: list[str]) -> int:
    healthy, text = check(argv[0] if argv else URL)
    print(text, flush=True)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
