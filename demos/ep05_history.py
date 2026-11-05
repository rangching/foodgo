"""第 5 集示範：同一張 A001，CRUD 只留下結果，事件留下過程。

    uv run python demos/ep05_history.py          # 全部跑一次
    uv run python demos/ep05_history.py crud     # CRUD：orders 表的那一列，每一步都蓋掉
    uv run python demos/ep05_history.py events   # 事件：A001 的事件流，只往後加
    uv run python demos/ep05_history.py fold     # fold：一個一個套上去，版本跟著算出來
    uv run python demos/ep05_history.py race     # 樂觀併發：兩個人同時取消 A001

不連資料庫、不用 Docker：CRUD 那一列用第 4 集 PostgresOrderRepository 真正寫進 orders 表的欄位（row_of），
事件存在記憶體版的事件儲存。時間固定成 2026-11-06 中午，每次跑出來都一樣。
"""
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

from foodgo.ordering.events import ItemAdded, OrderCancelled, OrderDrafted, OrderPlaced
from foodgo.ordering.fold import OrderState, evolve, fold
from foodgo.ordering.model import Address, Money, Order, OrderLine, OrderRuleViolation
from foodgo.ordering.postgres import row_of
from foodgo.shared.event_store import ConcurrencyConflict, InMemoryEventStore
from foodgo.shared.events import DomainEvent

TAIPEI = timezone(timedelta(hours=8))


def at(clock: str) -> datetime:
    hour, minute, second = map(int, clock.split(":"))
    return datetime(2026, 11, 6, hour, minute, second, tzinfo=TAIPEI)


def a001_events() -> list[DomainEvent]:
    """A001 的一生：12:00 開始點、12:01 送出、12:20 顧客取消。"""
    return [
        OrderDrafted(
            occurred_at=at("12:00:00"),
            order_id="A001",
            customer_id="C042",
            restaurant_id="R007",
            delivery_address=Address("信義路五段 7 號"),
            delivery_fee=Money(30),
            note="不要香菜",
        ),
        ItemAdded(occurred_at=at("12:00:20"), order_id="A001", name="牛肉麵", unit_price=Money(160), quantity=1),
        ItemAdded(occurred_at=at("12:00:35"), order_id="A001", name="紅茶", unit_price=Money(40), quantity=1),
        OrderPlaced(
            occurred_at=at("12:01:00"),
            order_id="A001",
            customer_id="C042",
            restaurant_id="R007",
            lines=(OrderLine("牛肉麵", Money(160)), OrderLine("紅茶", Money(40))),
            note="不要香菜",
        ),
        OrderCancelled(occurred_at=at("12:20:00"), order_id="A001", reason="顧客改變心意"),
    ]


# ---------------------------------------------------------------- 排版：中文字佔兩格
def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, size: int) -> str:
    return text + " " * (size - width(text))


def dishes(lines: tuple[OrderLine, ...]) -> str:
    return "、".join(f"{line.name} ×{line.quantity}" for line in lines) or "（還沒有）"


def describe(event: DomainEvent) -> str:
    match event:
        case OrderDrafted():
            return f"顧客 {event.customer_id}、餐廳 {event.restaurant_id}、{event.delivery_address.street}、外送費 {event.delivery_fee}、備註「{event.note}」"
        case ItemAdded():
            return f"{event.name} ×{event.quantity}，單價 {event.unit_price}"
        case OrderCancelled():
            return f"理由「{event.reason}」"
    return ""


# ---------------------------------------------------------------- 1. CRUD：只留結果
def crud() -> None:
    print("== CRUD：orders 表裡 A001 只有一列，每做一件事就蓋掉一次 ==")
    order = Order(
        order_id="A001",
        customer_id="C042",
        restaurant_id="R007",
        delivery_address=Address("信義路五段 7 號"),
        delivery_fee=Money(30),
        note="不要香菜",
    )
    steps = [
        ("12:00:00", "INSERT", lambda: None),
        ("12:00:20", "UPDATE", lambda: order.add_line("牛肉麵", Money(160))),
        ("12:00:35", "UPDATE", lambda: order.add_line("紅茶", Money(40))),
        ("12:01:00", "UPDATE", order.place),
        ("12:20:00", "UPDATE", lambda: order.cancel("顧客改變心意")),
    ]
    for clock, sql, action in steps:
        action()
        row = row_of(order)  # 第 4 集 PostgresOrderRepository.save() 寫進 orders 表的就是這些欄位
        lines = "、".join(f"{line['name']} ×{line['quantity']}" for line in row["lines"].obj)
        print(f"  {clock}  {sql}  status={pad(str(row['status']), 10)} lines=[{lines}]")

    print("\n最後留下來的就這一列（像 psql 的 \\x 顯示）：")
    row = {**row_of(order), "updated_at": f"2026-11-06 {clock}+08"}  # updated_at 由資料庫填 now()：最後一次 UPDATE 的時間
    print("-[ RECORD 1 ]----+" + "-" * 40)
    for column, value in row.items():
        if column == "lines":
            value = "、".join(f"{line['name']} ×{line['quantity']} {line['unit_price']} 元" for line in value.obj)
        print(f"{column:<16} | {value}")
    print("\n這一列答不出來：")
    print("  ・幾點送出的？——updated_at 只記最後一次（取消的時間）")
    print("  ・為什麼取消？——orders 表沒有放理由的欄位")
    print("  ・品項是一次點好，還是一個一個加的？")


# ---------------------------------------------------------------- 2. 事件：留下過程
def events() -> None:
    print("== 事件：A001 的事件流，只往後加，不改、不刪 ==")
    store = InMemoryEventStore()
    store.append("A001", a001_events(), expected_version=0)
    print(f" {pad('stream_id', 9)} | 版本 | 時間     | {pad('type', 14)} | 內容")
    for record in store.records("A001"):
        clock = record.occurred_at.astimezone(TAIPEI).strftime("%H:%M:%S")
        print(f" {pad(record.stream_id, 9)} | {record.version:>4} | {clock} | {pad(record.type, 14)} | {describe(record.event)}")
    print(f"\nA001 現在是第 {store.version('A001')} 版：流裡有 {store.version('A001')} 個事件。CRUD 那一列蓋掉的，這裡都還在。")


# ---------------------------------------------------------------- 3. fold：狀態是算出來的
def show(state: OrderState) -> str:
    return f"{pad(state.status.value, 9)} | {pad(dishes(state.lines), 18)} | {state.total.amount:>3} 元"


def folding() -> None:
    print("== fold：把事件一個一個套上去（evolve），每套一個，版本 +1 ==")
    history = a001_events()
    print(f" 版本 | {pad('套上的事件', 14)} | {pad('status', 9)} | {pad('品項', 18)} | 合計")
    state = None
    for event in history:  # fold 做的就是這個迴圈，只是不印出每一步
        state = evolve(state, event)
        print(f" {state.version:>4} | {pad(type(event).__name__, 14)} | {show(state)}")
    then, now = fold(history[:4]), fold(history)
    print(f"\nfold(前 4 個事件) → 12:01:00 送出那一刻：第 {then.version} 版，{then.status.value}，合計 {then.total}")
    print(f"fold(全部 {len(history)} 個)   → 現在：第 {now.version} 版，{now.status.value}，合計 {now.total}")


# ---------------------------------------------------------------- 4. 樂觀併發：兩個人同時寫
def to_order(state: OrderState) -> Order:
    """把算出來的狀態擺回 Order，才能用它的規則判斷能不能取消。第 6 集把這件事搬進 Repository。"""
    return Order.restore(
        order_id=state.order_id,
        customer_id=state.customer_id,
        restaurant_id=state.restaurant_id,
        delivery_address=state.delivery_address,
        delivery_fee=state.delivery_fee,
        note=state.note,
        lines=list(state.lines),
        status=state.status,
    )


def race() -> None:
    print("== 樂觀併發：顧客 App 和客服後台同時要取消 A001 ==")
    store = InMemoryEventStore()
    store.append("A001", a001_events()[:4], expected_version=0)

    customer = fold(store.load("A001"))
    support = fold(store.load("A001"))
    print(f"顧客 App：讀到第 {customer.version} 版（{customer.status.value}）")
    print(f"客服後台：讀到第 {support.version} 版（{support.status.value}）")

    order = to_order(customer)
    order.cancel("顧客改變心意")
    version = store.append("A001", order.pull_events(), expected_version=customer.version)
    print(f'顧客 App：cancel("顧客改變心意") → append(expected_version={customer.version}) → 寫進去了，現在第 {version} 版')

    order = to_order(support)
    order.cancel("餐廳太忙")  # 它讀到的還是 placed，所以 Order 說可以取消
    print(f'客服後台：cancel("餐廳太忙") → append(expected_version={support.version})')
    try:
        store.append("A001", order.pull_events(), expected_version=support.version)
    except ConcurrencyConflict as conflict:
        print(f"  → ConcurrencyConflict：{conflict}")

    support = fold(store.load("A001"))
    print(f'客服後台：重讀 → 第 {support.version} 版（{support.status.value}）→ cancel("餐廳太忙")')
    try:
        to_order(support).cancel("餐廳太忙")
    except OrderRuleViolation as rule:
        print(f"  → OrderRuleViolation：{rule}（什麼都不寫）")

    stream = "、".join(f"{r.version} {r.type}" for r in store.records("A001"))
    print(f"\nA001 的事件流：{stream}")
    print("OrderCancelled 只有一個——「已取消不能再取消」沒有被兩個人同時繞過去。")


PARTS = {"crud": crud, "events": events, "fold": folding, "race": race}


def main(args: list[str]) -> None:
    chosen = args or list(PARTS)
    if any(name not in PARTS for name in chosen):
        sys.exit(f"用法：python demos/ep05_history.py [{'|'.join(PARTS)}]")
    for i, name in enumerate(chosen):
        if i:
            print()
        PARTS[name]()


if __name__ == "__main__":
    main(sys.argv[1:])
