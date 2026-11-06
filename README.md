# 好食快送 FoodGo

《從 DDD 到 K8S：事件驅動微服務入門》的範例程式。整套課只做這一個系統，每集長一點。

```bash
git tag -l 'ep*'          # 每集的起點與終點
git checkout ep12-start   # 跟著第 12 集做（第 1 集是 ep01-start，以此類推）
```

逐步指令在上一層的 `steps/epNN.md`，每集的檢查在 `tests/epNN/`。

```bash
uv run pytest -q              # 跑到目前為止所有集數的檢查（沒開 Docker 時，整合測試顯示 skipped）
uv run lint-imports --no-logo # 邊界規則
```

## 把 API 跑起來（第 2 集起）

```bash
uv run fastapi dev            # 入口寫在 pyproject.toml 的 [tool.fastapi]
```

打開 <http://127.0.0.1:8000/docs> 就能試 `POST /orders`。不用 FastAPI CLI 也可以：`uv run uvicorn foodgo.app:app --reload`。
第 4 集起訂單存在 PostgreSQL，要先 `docker compose up -d`（見下面）。

## 看事件流過去（第 3 集；第 4 集起廚房改成獨立的 consumer，見下一節）

```bash
uv run fastapi dev                              # 下單後 GET /tickets 看廚房單
FOODGO_FAULT_KITCHEN=crash uv run fastapi dev   # 模擬廚房當機：下單照樣 201
```

伺服器的紀錄會印出「點餐：發布 OrderPlaced A001」「廚房：開廚房單 T001…」。廚房當機時，沒送到的事件在 <http://127.0.0.1:8000/bus/dead-letters>。

## 把事件交給 Kafka（第 4 集起）

先起 Kafka、Kafka UI、PostgreSQL（`compose.yaml`）：

```bash
docker compose up -d
docker compose ps             # kafka、kafka-ui、postgres 三個都要是 (healthy)
```

再開三個終端機，各跑一個程序：

```bash
uv run fastapi dev                         # API：訂單和要發的事件（outbox）寫在同一個交易
uv run python -m foodgo.ordering.relay     # relay：outbox → Kafka topic ordering.events
uv run python -m foodgo.kitchen.consumer   # 廚房：Kafka → 廚房單（consumer group kitchen）
```

| 網址 | 看什麼 |
|---|---|
| <http://127.0.0.1:8000/docs> | 下單、看廚房單 |
| <http://127.0.0.1:18080> | Kafka UI：topic、partition、訊息、consumer group 與 lag |

故障開關（示範用）：

| 環境變數 | 加在哪個程序 | 會怎樣 |
|---|---|---|
| `FOODGO_FAULT_RELAY=crash-after-send` | relay | 送到 Kafka 後、標記 `sent_at` 前當掉：重啟後同一則再送一次 |
| `FOODGO_FAULT_KITCHEN=crash` | 廚房 consumer | 收到 `OrderPlaced` 就當掉：訊息留在 Kafka，lag 變 1，修好重啟會補開 |

看資料庫：`docker compose exec postgres psql -U foodgo -c 'SELECT * FROM outbox;'`

測試：`uv run pytest -q` 會跑全部；Docker 沒開時，標了 `integration` 的測試會 skipped。
只跑整合測試：`uv run pytest -m integration -q`（用自己的資料庫 `foodgo_test` 與臨時 topic，不會動到練習的資料）。

埠號（15432、19092、18080）被占用時：`cp .env.example .env` 改埠號，Python 程式改用 `uv run --env-file .env …` 啟動。
全部清掉重來：`docker compose down -v`。

## 狀態是事件的總和（第 5 集，不用 Docker）

```bash
uv run python demos/ep05_history.py          # 同一張 A001：CRUD 的最後一列 vs 事件流、fold、兩個人同時取消
uv run python demos/ep05_history.py fold     # 只看其中一段：crud／events／fold／race
```

`foodgo.ordering.fold` 的 `evolve()`／`fold()` 從事件算出訂單狀態與版本號；`foodgo.shared.event_store` 是記憶體版的事件儲存（`expected_version` 對不上就丟 `ConcurrencyConflict`）。第 6 集換成 PostgreSQL。

## 事件溯源上 PostgreSQL（第 6 集）

第 6 集起 API 的訂單存在 `events` 表（只往後加），要用的時候 fold 回來；第 4 集的 `orders` 表不再寫。
第 4、5 集練習時留下的訂單不會搬過來，想從 A001 重新開始：`docker compose down -v && docker compose up -d`。

```bash
uv run python demos/ep06_event_store.py          # events 表、重播、兩個連線同時寫、快照、時光機（資料庫 foodgo_demo）
uv run python demos/ep06_event_store.py race     # 只看其中一段：events／replay／race／snapshot／asof
```

| 網址 | 看什麼 |
|---|---|
| <http://127.0.0.1:8000/orders/A001/events> | A001 的每一個事件：版本、名稱、時間、內容 |
| `http://127.0.0.1:8000/orders/A001?as_of=2026-11-06T15:00:00` | 時光機：那個時間點的 A001（沒寫時區就當臺北時間） |

看 events 表：`docker compose exec -e PGTZ=Asia/Taipei postgres psql -U foodgo -c 'SELECT position, stream_id, version, type, occurred_at FROM events;'`
（示範腳本的資料在另一個資料庫：`psql -U foodgo -d foodgo_demo`）。
有人比你先改了同一張訂單，API 回 409：重新讀取，再決定要不要做。

## 付款、廚房、外送接力：編舞式 Saga（第 7 集）

第 7 集起一張訂單要經過四個上下文：點餐送出 → 付款授權 → 廚房接單 → 外送指派外送員。沒有人指揮，每個 consumer 聽事件、做自己的事、公告結果。

一條指令跑完整條流程（會自己起 relay 與三個 consumer、下一張單、等到外送員指派，再查走到哪）：

```bash
uv run python demos/ep07_choreography.py              # A001：一路走到外送員 M01
uv run python demos/ep07_choreography.py decline      # 付款失敗的那一條：停在半路
uv run python demos/ep07_choreography.py trace A001   # 只查：4 張表、4 個 topic、3 個 consumer group
```

想自己一個一個開（每個一個終端機；先開 consumer、再下單。第 8 集起 consumer 預設聽編排器的命令，照這一節開要在三個 consumer 前面加 `FOODGO_SAGA=choreography`）：

```bash
uv run fastapi dev                            # 點餐：下單寫 events＋outbox（OrderPlaced、PaymentRequested、DeliveryRequested）
uv run python -m foodgo.ordering.relay        # outbox → ordering.events
uv run python -m foodgo.payment.consumer      # 付款：PaymentRequested → 假的金流服務 → payment.events
uv run python -m foodgo.kitchen.consumer      # 廚房：OrderPlaced＋PaymentAuthorized → kitchen.events
uv run python -m foodgo.delivery.consumer     # 外送：DeliveryRequested＋TicketAccepted → delivery.events
```

| 網址 | 看什麼 |
|---|---|
| <http://127.0.0.1:18080/ui/clusters/foodgo/all-topics> | 四個 topic：`ordering.events`、`payment.events`、`kitchen.events`、`delivery.events` |
| <http://127.0.0.1:18080/ui/clusters/foodgo/consumer-groups> | 三個 consumer group：`payment`、`kitchen`、`delivery`，lag 都是 0 |

故障開關（第 7 集新增）：

| 環境變數 | 加在哪個程序 | 會怎樣 |
|---|---|---|
| `FOODGO_FAULT_PAYMENT=decline` | 付款 consumer | 假的金流服務每一筆都拒絕：`PaymentFailed`，廚房不做了，訂單卻還是 placed |
| `FOODGO_FAULT_KITCHEN=reject` | 廚房 consumer | 付款過了也拒單：`TicketRejected`（第 8 集示範補償用） |

看各上下文自己的表：

```bash
docker compose exec postgres psql -U foodgo -c 'SELECT payment_id, order_id, amount, status, reason FROM payments;'
docker compose exec postgres psql -U foodgo -c 'SELECT ticket_id, order_id, status FROM tickets;'
docker compose exec postgres psql -U foodgo -c 'SELECT delivery_id, order_id, status, courier_id FROM deliveries;'
```

## 出錯時怎麼倒回去：編排式 Saga（第 8 集）

第 8 集起由編排器 `OrderSaga`（`foodgo.saga`）指揮：它收齊點餐的三則事實，送命令給付款、廚房、外送（`payment.commands`、`kitchen.commands`、`delivery.commands`），收回覆、決定下一步；失敗時倒回去補償（退款、取消訂單）。每一步都記在 `saga_log` 表。

一條指令跑一個情境（會自己起 relay、點餐、編排器、付款、廚房、外送六個程序，下一張新的單，等到 saga 走完）：

```bash
uv run python demos/ep08_orchestration.py              # 一路走到外送員指派：completed
uv run python demos/ep08_orchestration.py decline      # 付款失敗 → CancelOrder → cancelled
uv run python demos/ep08_orchestration.py reject       # 付款成功、餐廳拒單 → RefundPayment → CancelOrder → cancelled
uv run python demos/ep08_orchestration.py crash        # 廚房當機：逾時 → 重試 → 補償；修好重啟後廚房單也撤銷
uv run python demos/ep08_orchestration.py slow         # 廚房很慢：逾時 → 重試 → 回覆到了 → completed
uv run python demos/ep08_orchestration.py refund-fail  # 拒單＋退款失敗 → 重試 → needs_attention（人工處理）
uv run python demos/ep08_orchestration.py log A002     # 只看：A002 的 sagas、saga_log 與各上下文的表
```

想自己一個一個開（每個一個終端機）：

```bash
uv run fastapi dev                            # 點餐 API：下單寫 events＋outbox
uv run python -m foodgo.ordering.relay        # outbox → ordering.events
uv run python -m foodgo.ordering.consumer     # 點餐：聽 ordering.commands（CancelOrder）
uv run python -m foodgo.saga.consumer         # 編排器：聽四條事件 topic，送命令到四條命令 topic
uv run python -m foodgo.payment.consumer      # 付款：聽 payment.commands
uv run python -m foodgo.kitchen.consumer      # 廚房：聽 kitchen.commands
uv run python -m foodgo.delivery.consumer     # 外送：聽 delivery.commands
```

看 saga 走到哪、怎麼走過來的：

```bash
docker compose exec postgres psql -U foodgo -c "SELECT saga_id, state, waiting_for, attempts, reason FROM sagas;"
docker compose exec -e PGTZ=Asia/Taipei postgres psql -U foodgo -c "SELECT id, to_char(at, 'HH24:MI:SS.MS') AS at, state, action, message, detail FROM saga_log WHERE saga_id = 'A002' ORDER BY id;"
```

| 網址 | 看什麼 |
|---|---|
| <http://127.0.0.1:18080/ui/clusters/foodgo/all-topics> | 八個 topic：四條 `*.events`、四條 `*.commands` |
| <http://127.0.0.1:18080/ui/clusters/foodgo/consumer-groups> | 五個 consumer group：`saga`、`ordering`、`payment`、`kitchen`、`delivery` |

故障開關與設定（第 8 集新增）：

| 環境變數 | 加在哪個程序 | 會怎樣 |
|---|---|---|
| `FOODGO_FAULT_KITCHEN=slow` | 廚房 consumer | 每則命令想 `FOODGO_KITCHEN_SLOW_SECONDS` 秒（預設 4）才回 |
| `FOODGO_FAULT_REFUND=fail` | 付款 consumer | 每一筆退款都失敗：`RefundFailed` |
| `FOODGO_SAGA_TIMEOUT`、`FOODGO_SAGA_RETRIES` | 編排器 | 等回覆幾秒（預設 3）、逾時或退款失敗後再送幾次（預設 1） |
| `FOODGO_SAGA=choreography` | 付款、廚房、外送 consumer | 換回第 7 集的接力（要換先 `docker compose down -v`：同一個 consumer group 換 topic 會從頭讀） |

第 7 集的 `FOODGO_FAULT_PAYMENT=decline`、`FOODGO_FAULT_KITCHEN=reject|crash` 照用，現在編排器會接手補償。

## 讀寫分家：訂單追蹤（第 9 集）

第 9 集起多了**讀取端**（`foodgo.tracking`）：投影 consumer 聽五條事件 topic（四個上下文的 `*.events`，加上編排器的 `saga.events`），把一張訂單的進度排成 `order_tracking` 的一列；追蹤 API 與追蹤頁只讀這張表。

一條指令跑一個情境（會自己起第 8 集那六個程序＋投影，下一張新的單，一直查追蹤到走完）：

```bash
uv run python demos/ep09_tracking.py              # 下單後馬上查：404 更新中… → 已下單 → … → 外送中
uv run python demos/ep09_tracking.py slow         # 廚房想 2 秒才回：追蹤頁看得到一格一格亮起來
uv run python demos/ep09_tracking.py decline      # 付款失敗 → 已取消
uv run python demos/ep09_tracking.py reject       # 餐廳拒單 → 退款 → 已取消
uv run python demos/ep09_tracking.py refund-fail  # 退款一直失敗 → 人工處理中（編排器公告 SagaEscalated）
uv run python demos/ep09_tracking.py rebuild      # 砍掉 order_tracking、從 Kafka 重播，比對前後的 md5
uv run python demos/ep09_tracking.py show A002    # 只看：A002 在讀模型裡的樣子
```

想自己一個一個開：第 8 集那七個終端機，再加一個

```bash
uv run python -m foodgo.tracking.consumer     # 投影：五條事件 topic → order_tracking（consumer group tracking）
```

| 網址 | 看什麼 |
|---|---|
| <http://127.0.0.1:8000/track/A001> | 追蹤頁：每秒查一次，四格步驟條（點餐、付款、廚房、外送）；還沒投影到顯示「更新中…」 |
| <http://127.0.0.1:8000/orders/A001/tracking> | 追蹤 API（JSON）：一次查完；投影還沒讀到回 404＋`Retry-After: 1` |
| <http://127.0.0.1:18080/ui/clusters/foodgo/consumer-groups> | 多了 `tracking`（聽 5 個 topic） |

看讀模型、讀到哪：

```bash
docker compose exec postgres psql -U foodgo -c "SELECT order_id, status, payment, kitchen, delivery, courier_id FROM order_tracking ORDER BY order_id;"
docker compose exec postgres psql -U foodgo -c "SELECT topic, partition, last_offset FROM projection_positions ORDER BY topic, partition;"
docker compose exec postgres psql -U foodgo -c "SELECT count(*), md5(string_agg(t::text, '|' ORDER BY order_id)) FROM order_tracking t;"
```

砍掉重建（先 Ctrl-C 停掉投影 consumer；還開著的話會拒絕）：

```bash
uv run python -m foodgo.tracking.rebuild      # 清表＋忘記讀到哪（一個交易）→ 從 Kafka 重播 → 比對 md5
```

Kafka 和資料庫要一起清（`docker compose down -v`）：只清一邊的話，`projection_positions` 記的位置會對不上 topic。

## 依邊界拆服務：五個程序與 Jaeger（第 10 集）

第 10 集起 FoodGo 拆成五個程序（`foodgo.services`）：對外只有 Gateway（:8000），後面四個服務各有自己的資料庫；服務之間只透過 Kafka。`compose.yaml` 多了 Jaeger（`jaegertracing/jaeger:2.23.0`），一張訂單走過的每一段都串成一條 trace。

| 程序 | 啟動 | 埠 | 資料庫 |
|---|---|---|---|
| Gateway | `uv run python -m foodgo.services.gateway` | 8000 | 無 |
| 點餐服務（＋編排器、讀取端） | `uv run python -m foodgo.services.ordering` | 8101 | `foodgo_ordering` |
| 付款服務 | `uv run python -m foodgo.services.payment` | 8102 | `foodgo_payment` |
| 廚房服務 | `uv run python -m foodgo.services.kitchen` | 8103 | `foodgo_kitchen` |
| 外送服務 | `uv run python -m foodgo.services.delivery` | 8104 | `foodgo_delivery` |

一條指令起五個程序（第 9 集自己開著的 consumer、`fastapi dev` 先停掉：consumer group 名字一樣，會互相搶訊息）：

```bash
uv run python demos/ep10_services.py up        # 只起五個程序、印每個程序的紀錄（Ctrl-C 全部停）
uv run python demos/ep10_services.py           # 起五個程序 → 從 Gateway 下單 → 查追蹤到走完 → 印出 Jaeger 上的整條 trace
uv run python demos/ep10_services.py reject    # 餐廳拒單 → 退款 → 取消：還是同一條 trace
uv run python demos/ep10_services.py decline   # 付款失敗 → 取消
uv run python demos/ep10_services.py trace <trace_id>
```

| 網址 | 看什麼 |
|---|---|
| <http://127.0.0.1:8000/services> | Gateway 問後面四個服務的 `/healthz` |
| <http://127.0.0.1:8000/track/A001> | 追蹤頁（經過 Gateway 轉給點餐服務） |
| <http://127.0.0.1:16686/search?service=gateway&operation=POST%20%2Forders> | Jaeger UI：Service `gateway`、Span Name `POST /orders` → Find Traces；或右上角貼上回應 header 的 `X-Trace-ID`。Attributes 可以搜 `foodgo.order_id=A001` |
| <http://127.0.0.1:18080/ui/clusters/foodgo/all-topics/ordering.events/messages> | 每則訊息的 Headers 多了 `traceparent`、`correlation_id` |

看每個服務自己的資料庫：

```bash
docker compose exec postgres psql -U foodgo -c '\l foodgo*'
docker compose exec postgres psql -U foodgo -d foodgo_payment -c '\dt'
docker compose exec postgres psql -U foodgo -d foodgo_ordering -c "SELECT type, headers FROM outbox ORDER BY id LIMIT 3;"
```

第 4～9 集的單一程序（`uv run fastapi dev`、`python -m foodgo.kitchen.consumer`…）照樣能跑，用的是資料庫 `foodgo`；和第 10 集的服務用同一組 topic 與 consumer group 名字，**不要同時開**（換之前 `docker compose down -v`）。
埠號被占用：服務的埠用 `FOODGO_GATEWAY_PORT`、`FOODGO_ORDERING_PORT`…改；Jaeger 用 `.env` 的 `FOODGO_JAEGER_UI_PORT`、`FOODGO_OTLP_PORT`。

## 一次打包到處跑：容器（第 11 集）

第 11 集起 FoodGo 有自己的映像檔（`Dockerfile`）：**一個映像檔 `foodgo:0.11.0`、五個容器**（Gateway＋四個服務，只差 `command`）。`compose.yaml` 把它們放在 profile `app`：

```bash
docker compose up -d                             # 只起基礎設施（Kafka、Kafka UI、PostgreSQL、Jaeger）：第 4～10 集照舊用 uv 在本機跑程式
docker compose --profile app up -d --build --wait  # 建映像檔、起全部九個，等到全部 healthy
docker compose --profile app ps                  # 九個都要是 (healthy)
```

要 BuildKit（`Dockerfile` 用了 `RUN --mount=type=cache`）：Docker Desktop 內建；colima 要 `brew install docker-buildx`（`docker buildx version` 有版本號就對了）。

| 容器 | 映像檔 | 對外的埠 | 資料庫 |
|---|---|---|---|
| gateway | `foodgo:0.11.0` | `${FOODGO_GATEWAY_PORT:-8000}` → 8000（對外唯一的入口） | 無 |
| ordering | `foodgo:0.11.0` | 無（只有容器之間連得到 `ordering:8000`） | `foodgo_ordering` |
| payment | `foodgo:0.11.0` | 無 | `foodgo_payment` |
| kitchen | `foodgo:0.11.0` | 無 | `foodgo_kitchen` |
| delivery | `foodgo:0.11.0` | 無 | `foodgo_delivery` |

從 Gateway 下單、看 trace（和第 10 集一樣，只是五個程序換成容器）：

```bash
curl -si http://127.0.0.1:8000/orders -H 'Content-Type: application/json' \
  -d '{"customer_id":"C042","restaurant_id":"R007","lines":[{"name":"牛肉麵","unit_price":160,"quantity":1},{"name":"紅茶","unit_price":40,"quantity":1}],"delivery_fee":30,"note":"不要香菜","delivery_address":"信義路五段 7 號"}'
uv run python demos/ep10_services.py trace <回應的 X-Trace-ID>
```

| 網址 | 看什麼 |
|---|---|
| <http://127.0.0.1:8000/services> | Gateway 問四個服務容器的 `/healthz` |
| <http://127.0.0.1:16686/search?service=gateway> | Jaeger：一條 trace、5 個服務、27 個 span，和第 10 集一樣 |
| <http://127.0.0.1:18080/ui/clusters/foodgo/all-topics/ordering.events/messages> | Kafka UI：容器送出的訊息 |

故障開關從終端機帶進 compose（只會給那一個服務）：

```bash
FOODGO_FAULT_KITCHEN=crash docker compose --profile app up -d kitchen   # 換一個會當掉的廚房
docker compose --profile app ps kitchen                                 # 下一張單後約 30 秒：(unhealthy)
docker compose --profile app up -d kitchen                              # 換回正常的廚房
```

`unhealthy` 只是標出來，Docker 不會重開它；`restart: unless-stopped` 只在**程序結束**時重開（試試 `docker compose --profile app exec kitchen sh -c 'kill 1'`）。會把不健康的容器換掉的是第 12 集的 Kubernetes。

映像檔多大、有幾層：

```bash
docker image ls foodgo
docker history foodgo:0.11.0
```

記憶體：九個容器一起跑約 1.1 GB（Kafka 約 310 MB、Kafka UI 約 410 MB、PostgreSQL 約 65 MB、Jaeger 約 15 MB、五個 FoodGo 容器各約 55～60 MB）。Docker 至少給 4 GB 就夠；不夠時 Kafka 的 `KAFKA_HEAP_OPTS`、Kafka UI 的 `JAVA_OPTS` 可以再調小。

收拾（**要加 `--profile app`**：不加的話只會停基礎設施，五個 FoodGo 容器還留著）：

```bash
docker compose --profile app down        # 停掉；資料留著
docker compose --profile app down -v     # 連 Kafka 與資料庫一起清
docker image rm foodgo:0.11.0            # 刪掉映像檔（下次 --build 再建）
```

第 10 集的本機五個程序（`uv run python demos/ep10_services.py up`）和這一集的容器用同一組 topic、consumer group 名字與 Gateway 埠 8000，**不要同時開**。

## 交給 K8S 調度：kind 叢集（第 12 集）

第 12 集把整套 FoodGo 部署到本機的 Kubernetes（kind：每個節點是一個 Docker 容器）。要先裝 kind 與 kubectl（`brew install kind kubectl`），Docker 至少給 4 GB 記憶體。第 11 集的容器先收掉（`docker compose --profile app down -v`）：Gateway 一樣用本機的 8000。

```bash
kind create cluster --config kind-config.yaml        # 一個節點的叢集 foodgo（kubectl 的 context 是 kind-foodgo）
docker build -t foodgo:0.12.0 .                      # 映像檔（tag 跟著 foodgo.infra.service.VERSION）
kind load docker-image foodgo:0.12.0 --name foodgo   # 載進叢集：不經過 registry
kubectl config set-context --current --namespace=foodgo
kubectl apply -k k8s/                                # 整套：Kafka、PostgreSQL、Jaeger＋五個 FoodGo
kubectl get pods -w                                  # 等到全部 1/1 Running（服務會先 CrashLoopBackOff 兩三次，等 Kafka 與 PostgreSQL 好了就好）
```

| 網址 | 看什麼 |
|---|---|
| <http://127.0.0.1:8000/services> | Gateway（NodePort 30080 → 本機 8000）問四個服務的 `/healthz` |
| <http://127.0.0.1:26686/search?service=gateway> | Jaeger UI（NodePort 30686 → 本機 26686）：下單 A001，一條 trace、5 個服務、27 個 span |
| <http://127.0.0.1:28080> | Kafka UI：要的時候才裝 `kubectl apply -k k8s/kafka-ui`（多吃約 0.4 GB），看完 `kubectl delete -k k8s/kafka-ui` |

`k8s/` 的內容（`kubectl kustomize k8s/` 印出組好的全部）：

| 檔案 | 裡面有什麼 |
|---|---|
| `kustomization.yaml` | 把下面這些組起來、全部放進 namespace `foodgo`、`image: foodgo` 換成 `foodgo:0.12.0`（版本只寫這裡） |
| `config.yaml` | ConfigMap `foodgo`：`kafka:9092`、`http://jaeger:4318`、`http://ordering:8000`…（和第 11 集 compose 一模一樣） |
| `secret.yaml` | Secret `foodgo-db`：PostgreSQL 的密碼、四個服務各自的 `FOODGO_DATABASE_URL`（base64，練習用的假密碼） |
| `infra/` | `postgres`、`kafka`（StatefulSet＋1 GB 的 PVC）、`jaeger`（Deployment；UI 在 NodePort 30686）——教學用的最簡版本 |
| `apps/` | 五個 Deployment＋Service：`python -m foodgo.services.<名字>`、非 root（10001）、唯讀檔案系統、requests／limits、三種探針；廚房 3 個 |
| `kafka-ui/` | 另外裝的 Kafka UI |

常用的：

```bash
kubectl scale deployment kitchen --replicas=3                        # 臨時改個數（下次 apply 會照檔案改回來）
kubectl delete pod <廚房的 Pod>                                        # 看它被補回來
kubectl exec kafka-0 -- /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --group kitchen
kubectl set env deployment/kitchen FOODGO_FAULT_KITCHEN=crash        # 換成會當掉的廚房：下一張單後存活探針一直重開它
kubectl rollout undo deployment/kitchen                              # 退回上一版
kubectl rollout status deployment/kitchen
```

滾動更新到新版本：改 `foodgo.infra.service.VERSION`、`compose.yaml` 的 `image`、`k8s/kustomization.yaml` 的 `newTag` → `docker build -t foodgo:<新版> .` → `kind load docker-image foodgo:<新版> --name foodgo` → `kubectl apply -k k8s/`。

記憶體：整個 kind 節點約 2 GB（Kubernetes 自己約 0.9 GB、Kafka 約 480 MB、PostgreSQL 約 70 MB、七個 FoodGo 的 Pod 各約 55～60 MB）。

收拾：

```bash
kind delete cluster --name foodgo       # 整個叢集（連資料）一起刪
docker image rm foodgo:0.11.0 foodgo:0.12.0
```

叢集測試（`tests/ep12/test_ep12_cluster.py`，標 `kind`）要叢集開著才會跑；沒有叢集時自動略過。叢集名字不是 `foodgo` 的話設 `FOODGO_KIND_CLUSTER`。
