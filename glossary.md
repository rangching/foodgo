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
