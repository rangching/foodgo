"""事件儲存（記憶體版）：只能往後加，不能改、不能刪。第 6 集換成 PostgreSQL 的 events 表。

一張訂單一條事件流（stream），用訂單編號當 stream_id。流裡的事件從 1 開始編版本號。
寫入時要說「我讀到的是第幾版」（expected_version）：和現在的版本對不上，就是有人先寫了——整批不寫，丟 ConcurrencyConflict。
"""
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from foodgo.shared.events import DomainEvent


class ConcurrencyConflict(Exception):
    """樂觀併發：寫入時的版本和讀的時候不一樣。重讀、重新判斷，再決定要不要寫。"""

    def __init__(self, stream_id: str, expected: int, actual: int) -> None:
        super().__init__(f"{stream_id}：你讀到的是第 {expected} 版，現在已經是第 {actual} 版（有人先寫了）")
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class RecordedEvent:
    """事件儲存裡的一列。第 6 集的 events 表就是這幾個欄位（event 改存成 data jsonb）。"""

    stream_id: str  # 哪一條事件流：一張訂單一條，例如 A001
    version: int  # 這條流裡的第幾個事件：1、2、3…；同一條流不會有兩個同樣的版本
    type: str  # 事件名稱，例如 OrderPlaced
    occurred_at: datetime
    event: DomainEvent


class InMemoryEventStore:
    def __init__(self) -> None:
        self._streams: dict[str, list[RecordedEvent]] = {}
        self._lock = threading.Lock()  # 「比對版本」和「寫進去」要是同一步，中間不能插隊

    def version(self, stream_id: str) -> int:
        """這條流現在有幾個事件；還不存在就是 0。"""
        return len(self._streams.get(stream_id, []))

    def append(self, stream_id: str, events: Sequence[DomainEvent], *, expected_version: int) -> int:
        """接在第 expected_version 個事件後面，回傳寫完的版本。新的流要說 expected_version=0。"""
        with self._lock:
            stream = self._streams.setdefault(stream_id, [])
            if len(stream) != expected_version:
                raise ConcurrencyConflict(stream_id, expected_version, len(stream))
            for number, event in enumerate(events, start=expected_version + 1):
                stream.append(RecordedEvent(stream_id, number, type(event).__name__, event.occurred_at, event))
            return len(stream)

    def load(self, stream_id: str) -> list[DomainEvent]:
        """照版本順序讀出一條流的所有事件（交給 fold）。"""
        return [record.event for record in self._streams.get(stream_id, [])]

    def records(self, stream_id: str) -> list[RecordedEvent]:
        """連同版本號、事件名稱一起看（像在看資料表）。"""
        return list(self._streams.get(stream_id, []))
