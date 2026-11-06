# 好食快送 FoodGo

《從 DDD 到 K8S：事件驅動微服務入門》的範例程式。整套課只做這一個系統，每集長一點。

```bash
git tag -l 'ep*'          # 每集的起點與終點
git checkout ep09-start   # 跟著第 9 集做（第 1 集是 ep01-start，以此類推）
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
