# 第 11 集：FoodGo 的映像檔。一個映像檔、五個容器：Gateway 與四個服務是同一包程式（同一份 uv.lock），
# 只差啟動哪一個模組——由 compose.yaml 的 command 決定（第 12 集是 Deployment 的 command）。
#
#   docker build -t foodgo:0.11.0 .
#   docker run --rm -p 8000:8000 foodgo:0.11.0                       # 預設跑 Gateway
#   docker run --rm foodgo:0.11.0 python -m foodgo.services.kitchen  # 換一個服務
#
# 多階段建置：builder 有 uv、有快取，負責把套件裝進 /app/.venv；最後的映像檔只拿走 .venv。

# ---------------------------------------------------------------- 第一階段：builder
FROM python:3.12.15-slim-trixie AS builder

# uv 官方映像檔裡只有 uv 這個執行檔：複製過來就好，不用 pip install
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv

# 先編成 .pyc（容器啟動快一點）；快取掛載在另一個檔案系統，用複製不用連結；用映像檔的 Python，不另外下載
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# 1. 只裝依賴：只看 pyproject.toml 與 uv.lock。程式碼改了，這一層照樣用快取
#    --mount=type=cache：uv 下載過的套件留在建置機器上，下次建置不用重新下載；不會進映像檔
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project --no-editable

# 2. 再放程式碼、裝 foodgo 本身（--no-editable：裝成一般套件，之後就不需要 src/）
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------- 第二階段：真正要跑的映像檔
FROM python:3.12.15-slim-trixie

# 不用 root 跑：容器被打穿時，拿到的只是一個什麼都不能改的使用者
RUN groupadd --system --gid 10001 foodgo \
 && useradd --system --uid 10001 --gid foodgo --no-create-home --shell /usr/sbin/nologin foodgo

# 只拿 builder 裝好的虛擬環境：沒有 uv、沒有快取、沒有原始碼與測試。檔案屬於 root，foodgo 只能讀
COPY --from=builder /app/.venv /app/.venv

# 容器裡要聽 0.0.0.0（127.0.0.1 只有容器自己連得到）；五個程序在容器裡都聽 8000（每個容器有自己的網路）
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FOODGO_HOST=0.0.0.0 \
    FOODGO_GATEWAY_PORT=8000 \
    FOODGO_ORDERING_PORT=8000 \
    FOODGO_PAYMENT_PORT=8000 \
    FOODGO_KITCHEN_PORT=8000 \
    FOODGO_DELIVERY_PORT=8000

WORKDIR /app
USER 10001:10001
EXPOSE 8000

# 健康檢查：問自己的 /healthz（背景工作停了就 503 → unhealthy）。剛起來的 30 秒內每秒問一次、失敗不算數
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --start-interval=1s --retries=3 \
    CMD ["python", "-m", "foodgo.infra.healthcheck"]

# exec 形式（JSON 陣列）：python 就是 PID 1，docker stop 的 SIGTERM 直接送到它，服務照第 10 集的方式收尾
CMD ["python", "-m", "foodgo.services.gateway"]
