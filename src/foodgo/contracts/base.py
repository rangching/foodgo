"""上下文之間傳的訊息的共同格式：一個 JSON 信封，外面是型別、版本、編號、時間，裡面是資料。

第 3 集起只有事件（IntegrationEvent）；第 8 集多了命令（Command）。兩種信封一模一樣，差在名字的文法與對象。
"""
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, ClassVar, Self
from uuid import uuid4


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, kw_only=True)
class Message:
    """一則訊息：信封的格式、轉成 JSON、從 JSON 讀回來。事件和命令都是它。"""

    schema_version: ClassVar[int] = 1  # 拿掉欄位、改名、改意思時升版；只加欄位不用升

    event_id: str = field(default_factory=lambda: str(uuid4()))  # 這一則的編號（命令也用這個欄位：outbox、冪等都照它）
    occurred_at: str = field(default_factory=_now)  # ISO 8601，UTC

    def to_json(self, indent: int | None = None) -> str:
        data = asdict(self)
        envelope = {
            "type": type(self).__name__,
            "schema_version": self.schema_version,
            "event_id": data.pop("event_id"),
            "occurred_at": data.pop("occurred_at"),
            "data": data,
        }
        return json.dumps(envelope, ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> Self:
        envelope = json.loads(text)
        if envelope["type"] != cls.__name__:
            raise ValueError(f"這是 {envelope['type']}，不是 {cls.__name__}")
        if envelope["schema_version"] != cls.schema_version:
            raise ValueError(
                f"看不懂 {cls.__name__} 第 {envelope['schema_version']} 版（只認得第 {cls.schema_version} 版）"
            )
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in envelope["data"].items() if k in known}  # 不認得的欄位先略過
        return cls._from_data(event_id=envelope["event_id"], occurred_at=envelope["occurred_at"], **data)

    @classmethod
    def _from_data(cls, **data: Any) -> Self:
        """把 JSON 的資料變回訊息。有巢狀欄位（例如品項清單）的自己改寫這個方法。"""
        return cls(**data)


@dataclass(frozen=True, kw_only=True)
class IntegrationEvent(Message):
    """跨上下文的整合事件。欄位只用 JSON 認得的型別（字串、整數、清單），名稱一律過去式。"""


@dataclass(frozen=True, kw_only=True)
class Command(Message):
    """命令（第 8 集）：名稱是祈使句，指名一個接收者。接收者可以說不（回一則失敗的事件）。

    receiver：誰收這則命令；送到它的命令 topic（<receiver>.commands）。格式由接收的人維護：它說它接受哪些命令。
    """

    receiver: ClassVar[str] = ""
