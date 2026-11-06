# FoodGo 通用語言

同一個字在同一個上下文裡只有一個意思。程式裡的名字和這張表一致。

## 同一張「訂單」，四個上下文各叫什麼

| 上下文 | 套件 | 名稱 | 在乎什麼 | 不在乎什麼 | 子領域 |
|---|---|---|---|---|---|
| 點餐 | `foodgo.ordering` | 訂單 Order | 品項、數量、總金額、訂單狀態 | 菜怎麼做、誰去送 | 核心 |
| 付款 | `foodgo.payment` | 付款 Payment | 金額、授權、退款 | 點了哪些菜 | 通用 |
| 廚房 | `foodgo.kitchen` | 廚房單 Ticket | 菜名、數量、備註、出餐時間 | 價格、顧客地址 | 支援 |
| 外送 | `foodgo.delivery` | 配送單 Delivery | 取餐點、送達點、外送員 | 菜名、價格 | 核心 |

跨上下文只用**編號**指向對方（例如 `order_id`），不直接拿對方的物件。

## Event Storming 時間軸（領域事件）

依發生順序。一律過去式：是已經發生、不能改的事實。

| 順序 | 事件 | 程式名稱 | 上下文 |
|---|---|---|---|
| 1 | 訂單已送出 | `OrderPlaced` | 點餐 |
| 2 | 付款已授權 | `PaymentAuthorized` | 付款 |
| 3 | 餐廳已接單 | `TicketAccepted` | 廚房 |
| 4 | 外送員已指派 | `CourierAssigned` | 外送 |
| 5 | 餐點已備妥 | `MealReady` | 廚房 |
| 6 | 餐點已取貨 | `MealPickedUp` | 外送 |
| 7 | 餐點已送達 | `MealDelivered` | 外送 |

出錯時的事件：

| 事件 | 程式名稱 | 上下文 |
|---|---|---|
| 付款已失敗 | `PaymentFailed` | 付款 |
| 餐廳已拒單 | `TicketRejected` | 廚房 |
| 付款已退款 | `PaymentRefunded` | 付款 |
| 訂單已取消 | `OrderCancelled` | 點餐 |

## 點餐模型（第 2 集：戰術設計）

| 詞 | 程式名稱 | 種類 | 意思 |
|---|---|---|---|
| 金額 | `Money` | 值物件 | 新臺幣整數元，不能是負數；數字一樣就是同一個值 |
| 送達地址 | `Address` | 值物件 | 地址一樣就是同一個地方；要改地址就換一個新的 |
| 品項 | `OrderLine` | 值物件 | 菜名、單價、數量（至少 1）；小計＝單價 × 數量 |
| 訂單 | `Order` | 實體、聚合根 | 靠訂單編號認人；品項、金額、狀態都只能透過它的方法改 |
| 外送費 | `Order.delivery_fee` | 金額 | 每張訂單一筆，算進合計 |
| 小計／合計 | `Order.subtotal`／`Order.total` | 金額 | 小計＝品項小計相加；合計＝小計＋外送費。A001：200＋30＝230 |
| 訂單狀態 | `OrderStatus` | 列舉 | 草稿 `draft` → 已送出 `placed` → 已取消 `cancelled` |
| 訂單 Repository | `OrderRepository` | 介面 | 用編號拿訂單、存訂單、發下一個編號（A001、A002…） |

### 訂單的不變條件

不管從哪裡呼叫（API、測試、之後的事件處理），下面這些永遠成立。違反時 `Order` 丟出 `OrderRuleViolation`，狀態不變、也不會記下事件。

1. 送出（`place()`）時至少要有一個品項，不能送出空訂單。
2. 送出後不能再改品項（`add_line()` 只在草稿時可用），也不能再送出一次。
3. 合計一定等於品項小計相加再加外送費，由訂單自己算，外面不能直接改。
4. 取消（`cancel()`）的規則：
   - 只有已送出的訂單可以取消；草稿還沒成立，不叫取消。
   - 已取消的訂單不能再取消：`OrderCancelled` 只會發生一次。
   - 取消一定要寫理由，理由會寫進 `OrderCancelled.reason`。
   - 取消後一樣不能改品項。

之後的集數加上新的狀態（例如餐點已取貨）時，取消的規則會再收緊，並寫回這張表。

### 聚合發出的領域事件

| 方法 | 成功後記下的事件 |
|---|---|
| `Order(...)` | `OrderDrafted`（訂單草稿已建立；第 6 集起） |
| `Order.add_line()` | `ItemAdded`（品項已加入；第 6 集起） |
| `Order.place()` | `OrderPlaced`（訂單已送出） |
| `Order.cancel(reason)` | `OrderCancelled`（訂單已取消） |

事件先記在訂單裡，存檔之後由外面呼叫 `pull_events()` 一次取走；取走後就清空，不會發兩次。

### HTTP 怎麼回

| 情況 | 狀態碼 |
|---|---|
| 送出成功 | 201 |
| 請求格式不對（例如數量 0、少欄位） | 422（FastAPI 自動檢查） |
| 送出時違反不變條件（例如空訂單） | 422（模型擋下，API 照實回報） |
| 對既有訂單做不允許的事（例如取消兩次） | 409 |
| 讀完到寫之間有人先改了這張訂單（`ConcurrencyConflict`，第 6 集） | 409 |
| 找不到訂單 | 404 |

## 事件驅動（第 3 集：從呼叫到通知）

### 命令與事件

| | 命令 Command | 事件 Event |
|---|---|---|
| 文法 | 祈使句：「開一張廚房單」`create_ticket()` | 過去式：「訂單已送出」`OrderPlaced` |
| 對象 | 指定一個接收者，發的人要認識它 | 不指定，0 到很多個訂閱者，發的人不用認識 |
| 能不能拒絕 | 可以：接收者可以丟例外、回錯誤 | 不能：事情已經發生了，只能決定自己要怎麼反應 |
| 發的人等不等 | 等結果，對方掛了自己也跟著失敗 | 不等，交出去就好 |

### 兩種事件

| | 領域事件 Domain Event | 整合事件 Integration Event |
|---|---|---|
| 放在哪 | `foodgo.<上下文>.events` | `foodgo.contracts`（共同語言） |
| 誰看得到 | 只有自己的上下文 | 任何訂閱的上下文 |
| 可以放什麼 | 內部的東西都可以（例如帶 `Money` 的品項） | 只放 JSON 認得的型別，只放下游需要的事實 |
| 能不能隨便改 | 可以，改了只影響自己 | 不行，有人照著讀；要守版本規則 |

點餐的 `OrderPlaced` 領域事件帶著含單價的品項；`foodgo.ordering.integration.to_integration()` 把它翻成 `contracts.OrderPlaced`，翻譯時把價格留在點餐裡。
廚房的 `MealReady` 領域事件（第 1 集）同理；廚房是支援子領域，模型簡單，這一集由 `Kitchen.mark_ready()` 直接公告 `contracts.MealReady`。

### 共同語言 Published Language（`foodgo.contracts`）

上下文之間只透過這裡的整合事件說話，彼此不 import。`foodgo.contracts` 只用標準函式庫，不認識任何上下文、共用核心或組裝點（第 4 條邊界規則）。
每份格式由發布的上下文維護：`contracts/ordering.py` 歸點餐、`contracts/kitchen.py` 歸廚房。

每一則整合事件都是同一種 JSON 信封：

```json
{
  "type": "OrderPlaced",
  "schema_version": 1,
  "event_id": "8b0e…",
  "occurred_at": "2026-11-06T02:15:08+00:00",
  "data": {"order_id": "A001", "restaurant_id": "R007",
           "items": [{"name": "牛肉麵", "quantity": 1}, {"name": "紅茶", "quantity": 1}],
           "note": "不要香菜"}
}
```

| 欄位 | 意思 |
|---|---|
| `type` | 事件名稱，過去式，和類別名稱一樣 |
| `schema_version` | `data` 的格式第幾版 |
| `event_id` | 這一則事件的編號（UUID 字串）；點餐翻譯時沿用領域事件的編號 |
| `occurred_at` | 事情發生的時間，ISO 8601、UTC |
| `data` | 事件本身的資料 |

### 整合事件放什麼、不放什麼

原則：只放訂閱者**現在**要用的事實。加欄位很容易，拿掉欄位很難——一旦發布，就有人照著讀。

| 事件 | 發布 | 訂閱 | 放 | 不放（為什麼） |
|---|---|---|---|---|
| `OrderPlaced` v1 | 點餐 | 廚房 | `order_id`、`restaurant_id`、`items`（菜名、份數）、`note` | 單價、外送費、合計：廚房不看價格。`customer_id`、送達地址：廚房用不到，個資能少傳就少傳 |
| `MealReady` v1 | 廚房 | （練習：外送） | `order_id`、`ticket_id`、`restaurant_id`（到哪裡取餐） | 菜名、備註：外送不在乎菜怎麼做 |

`OrderCancelled` 目前沒有別的上下文要聽，先不放進共同語言；等有人需要時再加。之後付款要金額、外送要地址時，也照同一條原則決定加在哪裡。

### 版本規則（`schema_version`）

1. **只加欄位**：不升版。舊的訂閱者略過不認得的欄位，照樣能讀（寬容讀取）。
2. **拿掉欄位、改名、改型別、改意思**：升 `schema_version`。訂閱者遇到不認得的版本就拒收，丟 `ValueError`，不會讀錯。
3. `type` 不對（拿 `OrderPlaced` 當 `MealReady` 讀）也一樣拒收。

### 事件匯流排 `EventBus`（`foodgo.shared.bus`）

| 詞 | 程式名稱 | 意思 |
|---|---|---|
| 發布 | `bus.publish(event)` | 把事件交給匯流排；發布者不知道、也不用知道誰在聽 |
| 訂閱 | `bus.subscribe(事件類別, 處理函式)` | 「這種事件發生時叫我」。訂閱關係寫在組裝點 `app.py` |
| 處理函式 | 例如 `Kitchen.on_order_placed` | 收到事件後做自己的事；名稱一律 `on_事件` |
| 沒送到的事件 | `bus.dead_letters`（`GET /bus/dead-letters`） | 處理函式丟例外時，事件、處理函式與錯誤記在這裡，不傳回發布者 |

匯流排先把事件轉成 JSON，再替每個訂閱者各還原一份：上下文之間只傳資料，不傳物件。

這一集的限制（第 4 集用 Kafka 解決）：

- 只在同一個程序裡：程式一關，沒處理完的事件就不見了。
- `dead_letters` 只是記下來，不會重試；廚房恢復後也不會補開那張廚房單。
- 訂閱者還是在下單的同一個請求裡執行：廚房慢，下單就跟著慢。

### 廚房單（第 3 集）

| 詞 | 程式名稱 | 種類 | 意思 |
|---|---|---|---|
| 廚房單 | `Ticket` | 實體 | 收到 `OrderPlaced` 就開一張，編號 T001、T002…；只記 `order_id`，不認識點餐的物件 |
| 菜 | `Dish` | 值物件 | 菜名與份數，沒有價格 |
| 廚房單狀態 | `TicketStatus` | 列舉 | 已收單 `received` → 餐點已備妥 `ready` |
| 出餐 | `Kitchen.mark_ready()`（`POST /tickets/{id}/ready`） | 方法 | 廚房單標成備妥，公告 `MealReady`；出過餐的不能再出一次（409） |
| 廚房當機 | `KitchenDown`（`FOODGO_FAULT_KITCHEN=crash`） | 故障注入 | 開廚房單時丟例外，用來示範「廚房掛了，下單照樣成功」 |

## 可靠地發與收（第 4 集：把事件交給 Kafka）

### Kafka 的詞

| 詞 | 意思 | FoodGo 裡 |
|---|---|---|
| broker | 存訊息、發訊息的 Kafka 伺服器 | 一台，KRaft 模式（自己兼 controller，不用 ZooKeeper） |
| topic | 一條有名字、只會往後加的訊息紀錄；讀了也不會刪 | `ordering.events`：點餐的整合事件都在這裡（`contracts.ORDERING_TOPIC`） |
| partition | topic 切成幾段，各自有順序；同一個 key 永遠進同一段 | 3 段；key＝`order_id`，同一張訂單的事件不會亂序 |
| offset | 訊息在 partition 裡的位置（0、1、2…） | 廚房讀到哪裡，就記到哪個 offset |
| consumer group | 一組一起讀同一個 topic 的程序，每段 partition 只交給組裡一個成員；讀到哪裡（offset）記在組的名字下 | `kitchen`；第 7 集付款、外送各自一組，互不影響 |
| 提交 commit | 告訴 Kafka「這一則我處理完了」 | 處理完才提交；當掉沒提交，重啟後同一則會再收到一次 |
| lag | 還沒處理的訊息數＝最新 offset − 已提交的 offset | Kafka UI 的 Consumers 頁；廚房停掉時會變大，重啟後回到 0 |

### 雙寫問題

「先寫資料庫、再送 Kafka」是兩個系統、兩次寫入，中間沒有交易包住：

| 順序 | 中間當掉會怎樣 |
|---|---|
| 先 commit 訂單，再送事件 | 訂單成立了，事件沒送出去：廚房永遠不知道（掉事件） |
| 先送事件，再 commit 訂單 | 廚房開了單，訂單卻沒存成功（幽靈事件） |

### Transactional Outbox

| 詞 | 程式名稱 | 意思 |
|---|---|---|
| outbox | `outbox` 表（`foodgo.infra.outbox`） | 要發的事件先寫進點餐自己的資料庫，**和訂單同一個交易**：要嘛都成功、要嘛都沒發生 |
| 存訂單 | `PostgresOrderRepository.save()` | 一個交易裡：寫 `orders`，再把 `pull_events()` 翻成整合事件寫進 `outbox`；`OrderCancelled` 不公告，不寫 |
| relay | `python -m foodgo.ordering.relay`（`outbox.relay_once()`） | 另一個程序：把 `sent_at IS NULL` 的列照順序送到 Kafka，Kafka 確認收到才標記 `sent_at` |
| 重送 | `FOODGO_FAULT_RELAY=crash-after-send` | relay 送出後、標記前當掉：重啟會再送一次，Kafka 上出現同一則的兩份 |

訊息格式：key＝`order_id`；value＝第 3 集的 JSON 信封（`to_json()`，原封不動）；header 帶 `event_id`、`type`（方便在 Kafka UI 上看）。

### 至少一次送達＋冪等消費者

- **至少一次（at-least-once）**：relay 和 consumer 都是「做完才記帳」，中間當掉就重做。不會掉，但可能重複。
- **冪等（idempotent）**：同一則訊息處理一次和處理兩次，結果一樣。重複交給消費者自己擋。
- 廚房用 `event_id` 判重：`processed_messages`（`consumer`、`event_id` 是主鍵）。開廚房單和記下「這則處理過了」寫在**同一個交易**（`PostgresTicketRepository.add()`），重送幾次都只開一張。

| 詞 | 程式名稱 | 意思 |
|---|---|---|
| 廚房 consumer | `python -m foodgo.kitchen.consumer`（consumer group `kitchen`） | 讀 `ordering.events`，收到 `OrderPlaced` 開廚房單；其他事件略過 |
| 處理過的訊息 | `processed_messages`（`foodgo.infra.inbox`） | 冪等消費者的帳本：哪個 consumer 處理過哪一則 `event_id` |
| 處理過了 | `Kitchen.on_order_placed()` | 這則 `event_id` 記過了，就印「處理過了」、不再開單 |
| 廚房當機 | `FOODGO_FAULT_KITCHEN=crash` | consumer 收到就當掉、不提交；訊息留在 Kafka，修好重啟會補開那張單 |

第 3 集匯流排的三個限制，這一集的答案：

- 程序一關事件就不見 → 事件先在 PostgreSQL 的 outbox，再到 Kafka，兩邊都存在硬碟上。
- 不會重試 → consumer 沒提交就會再收到；relay 沒標記就會再送。
- 廚房慢下單也慢 → API 只寫資料庫；relay、廚房 consumer 是另外的程序。

## 事件溯源（第 5 集：狀態是事件的總和）

### CRUD 只留結果，事件留下過程

| | CRUD（第 2～4 集的 `orders` 表） | 事件溯源 Event Sourcing |
|---|---|---|
| 存什麼 | 現在長怎樣：一張訂單一列，每次 `UPDATE` 蓋掉舊的 | 發生過哪些事：一張訂單一串事件，只往後加 |
| A001 留下 | 最後一列：`cancelled`、兩個品項、`updated_at` 是取消的時間 | 5 個事件：建立草稿、加牛肉麵、加紅茶、送出、取消（含理由） |
| 現在的狀態 | 直接讀那一列 | 用算的：`state = fold(events)` |
| 過去某一刻 | 不知道，除非另外寫歷史表 | 只套前面幾個事件：`fold(events[:4])` 就是送出那一刻 |
| 改錯了 | 舊值被蓋掉就沒了 | 不改舊事件，再加一個新事件更正 |

第 4 集的 `orders` 表沒有放取消理由的欄位，`OrderCancelled` 也不進 outbox：在 CRUD 的世界裡，「顧客改變心意」這句話存檔後就不見了。

### 點餐的事件流

Order 的每個動作對應一個事件，照順序套上去就能算回整張訂單（第 6 集起由 `Order` 的方法發出，這一集先手寫）：

| 順序 | 事件 | 程式名稱 | 對應的動作 | 帶什麼 | `evolve` 怎麼改狀態 |
|---|---|---|---|---|---|
| 1 | 訂單草稿已建立 | `OrderDrafted` | `Order(...)` | 訂單編號、顧客、餐廳、送達地址、外送費、備註 | 從無到有：`draft`、還沒有品項 |
| 2… | 品項已加入 | `ItemAdded` | `add_line()` | 菜名、單價、數量 | 品項接在後面 |
| | 訂單已送出 | `OrderPlaced` | `place()` | （第 1 集就有） | `placed`；品項以 `ItemAdded` 為準，`OrderPlaced.lines` 是給廚房的快照 |
| | 訂單已取消 | `OrderCancelled` | `cancel(reason)` | 理由 | `cancelled` |

`OrderDrafted`、`ItemAdded` 是點餐自己的領域事件，不進 Event Storming 的 11 張便利貼、也不對外公告（`to_integration()` 回 `None`，不寫進 outbox）。

### 詞

| 詞 | 程式名稱 | 意思 |
|---|---|---|
| 事件溯源 | Event Sourcing | 只存事件，狀態用事件算出來 |
| 狀態 | `OrderState`（`foodgo.ordering.fold`） | 某一刻的訂單；frozen dataclass，算出來就不能改 |
| 演進 | `evolve(state, event) -> state` | 套上一個事件、回傳新的狀態。純函式：不碰資料庫、不看時鐘、不改舊狀態；不檢查業務規則（事件是已經發生的事實） |
| 摺疊 | `fold(events) -> state` | 從第一個事件套到最後一個（`reduce(evolve, events, None)`）；沒有事件回 `None`；認不得的事件丟 `TypeError`，不默默略過 |
| 事件流 stream | `stream_id`（訂單編號，例如 `A001`） | 同一張訂單的所有事件，照發生順序排好 |
| 版本 version | `OrderState.version`、`RecordedEvent.version` | 這條流的第幾個事件，從 1 開始；狀態的版本＝已經套用了幾個事件；還沒有事件的流是第 0 版 |
| 事件儲存 | `InMemoryEventStore`（`foodgo.shared.event_store`） | 只能 `append`、`load`，沒有改、沒有刪；第 6 集換成 PostgreSQL 的 `events` 表 |
| 一列事件 | `RecordedEvent` | `stream_id`、`version`、`type`、`occurred_at`、事件本身；`(stream_id, version)` 不會重複 |
| 預期版本 | `append(stream_id, events, expected_version=…)` | 「我讀到的是第幾版」；新的流是 0 |
| 樂觀併發 | `ConcurrencyConflict` | 先不鎖，寫的時候才比對版本：對不上就是有人先寫了，整批不寫。重讀、用新的狀態重新判斷規則，再決定要不要寫 |

為什麼要樂觀併發：顧客 App 和客服後台同時讀到第 4 版（`placed`），都覺得可以取消。沒有 `expected_version` 的話兩個 `OrderCancelled` 都會寫進去——「已取消不能再取消」就被繞過了。有了它，第二個人會拿到 `ConcurrencyConflict`，重讀後看到 `cancelled`，`Order` 的規則就擋下來。

### 什麼時候不該用事件溯源

事件溯源的代價：要多寫事件與 `evolve`、事件格式一旦存下去就要一直讀得懂（升版、轉換）、查詢要另外做讀模型（第 9 集）、「改錯資料」不能直接 `UPDATE`。值得付這個代價的，是**過程本身有意義**的東西。

| FoodGo 裡的東西 | 用什麼 | 為什麼 |
|---|---|---|
| 訂單（點餐） | 事件溯源（第 6 集） | 流程長、狀態多、會被併發修改；「什麼時候送出、為什麼取消」客服與對帳都要查；後面的 Saga、追蹤頁都靠這串事件 |
| 菜單、菜價 | CRUD | 店家改價格就是改價格，沒人問「上週二下午這道菜的說明改過幾次」；訂單需要的價格已經寫在 `ItemAdded.unit_price` 裡 |
| 餐廳資料（店名、地址、營業時間） | CRUD | 就是一張資料表；要留修改紀錄的話，加一張稽核表就夠了 |
| 顧客的帳號資料 | CRUD | 個資要能真的改、真的刪；事件只能往後加，刪不掉 |
| 廚房單 | CRUD（第 4 集的 `tickets`） | 支援子領域，狀態只有收單 → 備妥；它要的過程已經在 Kafka 的事件裡 |
| 報表、看板 | 讀模型（第 9 集） | 從事件投影出來的表，壞了就砍掉重建 |

判斷的問題：要不要回答「當時是什麼樣子、怎麼變成這樣」？會不會有人同時改同一筆？事件本身是不是業務上的事實？三個都是「否」，CRUD 就好。

## 事件溯源上 PostgreSQL（第 6 集：重播、快照、時光機）

### 存的地方：`events` 表與 `snapshots` 表

| 欄位 | 意思 |
|---|---|
| `position` | `bigserial`：所有事件流排在同一條線上的第幾個（第 9 集投影照這個順序讀） |
| `stream_id`、`version` | 哪一條事件流（訂單編號）、第幾個事件；`UNIQUE (stream_id, version)`：同一條流不會有兩個第 5 版 |
| `event_id` | 事件自己的編號（`UNIQUE`）；整合事件沿用它，廚房用它判重 |
| `type`、`data` | 事件名稱與內容（`jsonb`）。基礎設施只認這兩個，不認識事件類別 |
| `occurred_at` | 事件發生的時間：時光機照這個查 |

`events` 表**只能往後加**：`UPDATE`、`DELETE`、`TRUNCATE` 都由資料庫的觸發器擋下來（「事件只能往後加，不能改、不能刪」）。寫錯了就再加一個更正的事件。

`snapshots` 表（`stream_id`、`version`、`state jsonb`）放某一版的狀態。快照只是捷徑：刪掉了照樣能從事件算回來；`OrderState` 改了欄位，舊快照刪掉重建就好，事件才是真相。

### 誰負責什麼

| 層 | 程式 | 做什麼 |
|---|---|---|
| 基礎設施 | `foodgo.infra.event_store` | SQL：`append`（比對版本、寫入）、`read`（照版本讀，可以接在快照後面、可以只讀某個時間以前）、`save_snapshot`、`latest_snapshot`。只認 type＋data，版本對不上丟自己的 `VersionConflict` |
| 點餐 | `foodgo.ordering.codec` | 事件 ↔ `(type, data)`、狀態 ↔ 快照 JSON：`Money` 攤成整數元、`Address` 攤成字串。純函式；認不得的 type 丟 `TypeError` |
| 點餐 | `PostgresEventStore`（`foodgo.ordering.event_sourcing`） | 第 5 集 `InMemoryEventStore` 的 PostgreSQL 版，介面一樣；把 `VersionConflict` 翻成第 5 集的 `ConcurrencyConflict` |
| 點餐 | `EventSourcedOrderRepository` | 第 2 集的 `OrderRepository`（`next_id`／`get`／`save`），存的是事件；多了 `load`、`take_snapshot`、`load_as_of`、`history` |

第 5 條邊界規則「基礎設施不認識任何上下文」照樣守著；第 6 條「點餐的領域模型不碰基礎設施」：`model`、`events`、`fold`、`codec` 不能 import `foodgo.infra`、`foodgo.contracts`。

### 詞

| 詞 | 程式名稱 | 意思 |
|---|---|---|
| 重播 replay | `fold(store.load("A001"))` | 照版本順序讀出一條流的事件，一個一個 `evolve` 回現在的狀態 |
| 從狀態擺回聚合 | `Order.from_state(state)` | 取代第 4 集的 `Order.restore()`：Repository 先 `fold` 出 `OrderState`，再擺回 `Order`；不檢查規則、不記事件 |
| 訂單的版本 | `Order.version` | 套用了幾個事件（含還沒存的）。存的時候 `expected_version = 版本 − 還沒存的事件數` |
| 存訂單 | `EventSourcedOrderRepository.save(order)` | 一個交易：新事件 `append` 到 `events`，要公告的（`OrderPlaced`）寫進 `outbox`。一起成功、一起失敗 |
| 併發衝突 | `ConcurrencyConflict` → HTTP 409 | 讀完到寫之間有人先寫了。兩個連線同時寫第 5 版時，後寫的那個會在 `UNIQUE` 索引上等先寫的交易結束，再被擋下。API 回 409，請呼叫的人重新讀取、再決定（不自動重試：重讀之後規則可能已經不讓你做了） |
| 快照 snapshot | `take_snapshot(order_id)`、`load(order_id)` | 把某一版的狀態存起來；載入時從最新的快照接著 `fold(後面的事件, 快照)`。`Loaded.events_read` 看得到讀了幾個事件 |
| 時光機 | `load_as_of(order_id, when)`、`GET /orders/{id}?as_of=…` | 只 `fold` 那個時間點以前發生的事件（`occurred_at <= when`）。時間要帶時區；API 沒寫時區就當臺北時間。時光機不用快照，一律從頭算 |
| 事件歷史 | `history(order_id)`、`GET /orders/{id}/events` | 一張訂單的每一個事件：版本、名稱、時間、內容 |

記憶體版的 `InMemoryOrderRepository` 只記現在的樣子：`as_of` 與 `/events` 回 501。
