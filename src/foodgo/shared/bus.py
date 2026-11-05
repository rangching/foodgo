"""行程內的事件匯流排：發布者不知道誰在聽，訂閱者出錯也不會傳回發布者。

只認「能變成 JSON、也能從 JSON 變回來」的訊息，不認識任何上下文。
第 4 集換成 Kafka：發布端改寫 outbox、訂閱端改成 consumer，「發一則 JSON、各自訂閱」的形狀不變。
"""
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, Self


class Message(Protocol):
    """foodgo.contracts 的整合事件都符合這個形狀。"""

    def to_json(self) -> str: ...

    @classmethod
    def from_json(cls, text: str) -> Self: ...


Handler = Callable[[Any], None]


@dataclass(frozen=True)
class DeadLetter:
    """沒送到的事件：哪一則、交給誰、為什麼失敗。"""

    message: str  # 原封不動的 JSON
    handler: str
    error: str


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Handler]] = defaultdict(list)
        self.dead_letters: list[DeadLetter] = []

    def subscribe(self, message_type: type[Message], handler: Handler) -> None:
        self._handlers[message_type].append(handler)

    def publish(self, message: Message) -> None:
        payload = message.to_json()  # 跨上下文只傳資料，不傳物件
        for handler in self._handlers[type(message)]:
            try:
                handler(type(message).from_json(payload))  # 每個訂閱者拿到自己的一份
            except Exception as error:  # 訂閱者的錯留在匯流排這一層，不傳回發布者
                name = getattr(handler, "__qualname__", repr(handler))
                reason = f"{type(error).__name__}: {error}"
                self.dead_letters.append(DeadLetter(payload, name, reason))
                print(f"匯流排：{name} 處理 {type(message).__name__} 失敗（{reason}），放進 dead letters", flush=True)
