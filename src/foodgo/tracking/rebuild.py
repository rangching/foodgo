"""砍掉讀模型、從事件重建。先停掉投影 consumer（Ctrl-C），再跑：

    uv run python -m foodgo.tracking.rebuild

  1. 記下現在的樣子：列數＋整張表的 md5 指紋
  2. 一個交易：清空 order_tracking、忘記讀到哪（projection_positions 裡 order_tracking 的那幾列）
  3. 重播：五條 topic 的每個 partition 從第一則讀到現在最新（不加入 consumer group），一則一則投影
  4. 再算一次指紋：一樣＝重建出來的資料一模一樣
之後照常起投影 consumer：它從 projection_positions 記的下一則接著讀。

為什麼從 Kafka 重播、不從 events 表：events 表只有點餐的事件流；付款、廚房、外送、編排器的事實只在 Kafka 上。
"""
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from foodgo.infra import config, db, kafka
from foodgo.tracking.consumer import GROUP, LISTENS, handle
from foodgo.tracking.postgres import SCHEMAS, PostgresTrackingRepository


@dataclass(frozen=True)
class Rebuilt:
    before: tuple[int, str]  # 重建前：(列數, md5)
    cleared: tuple[int, int]  # 清掉了：(幾列, 幾個 partition 的位置)
    replayed: int  # 重播了幾則訊息
    after: tuple[int, str]  # 重建後：(列數, md5)

    @property
    def same(self) -> bool:
        return self.before == self.after


def rebuild(tracking: PostgresTrackingRepository, *, listens: Sequence[str] = LISTENS) -> Rebuilt:
    before = tracking.fingerprint()
    cleared = tracking.clear()
    replayed = kafka.replay(listens, lambda message: handle(tracking, message, quiet=True), start_at=tracking.start_at)
    return Rebuilt(before, cleared, replayed, tracking.fingerprint())


def main() -> None:
    url = config.database_url()
    db.create_tables(url, *SCHEMAS)
    members = kafka.active_members(GROUP)
    if members:
        sys.exit(f"投影 consumer 還在跑（consumer group {GROUP} 有 {members} 個成員）：先 Ctrl-C 停掉它，再重建")
    tracking = PostgresTrackingRepository(url)
    result = rebuild(tracking)
    (rows, digest), (cleared, partitions) = result.before, result.cleared
    print(f"重建前：order_tracking {rows} 列，指紋 {digest}")
    print(f"砍掉：order_tracking 清掉 {cleared} 列、projection_positions 清掉 {partitions} 個 partition 的位置（同一個交易）")
    print(f"重播：{'、'.join(LISTENS)} 從頭讀到最新，{result.replayed} 則")
    print(f"重建後：order_tracking {result.after[0]} 列，指紋 {result.after[1]}")
    print("一模一樣" if result.same else "不一樣：重建前的表和事件算出來的對不上（被改過，或投影還沒追上）——以重建後的為準")


if __name__ == "__main__":
    main()
