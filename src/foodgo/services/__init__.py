"""第 10 集：依邊界拆成服務。每個模組是一個可以單獨啟動的程序（自己的埠、自己的資料庫），也是它的組裝點：

    uv run python -m foodgo.services.gateway     # :8000  對外唯一的入口，看路徑轉給後面的服務
    uv run python -m foodgo.services.ordering    # :8101  點餐（＋編排器、讀取端）   資料庫 foodgo_ordering
    uv run python -m foodgo.services.payment     # :8102  付款                     資料庫 foodgo_payment
    uv run python -m foodgo.services.kitchen     # :8103  廚房                     資料庫 foodgo_kitchen
    uv run python -m foodgo.services.delivery    # :8104  外送                     資料庫 foodgo_delivery

服務之間不 import 彼此、不讀彼此的表、不互相呼叫 HTTP：只透過 Kafka 上的共同語言（foodgo.contracts）。
第 12～16 條邊界規則守著：每個服務只組裝自己的上下文；Gateway 只認得網址。
這個檔刻意不 import 任何服務：import foodgo.services.payment 時，不會順便把點餐也載進來。
"""
