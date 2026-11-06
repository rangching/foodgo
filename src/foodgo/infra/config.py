"""連線設定：預設對到 compose.yaml 的埠號；要改就設環境變數（或 .env＋uv run --env-file .env）。"""
import os


def postgres_url(database: str) -> str:
    """compose 那台 PostgreSQL 上的某一個資料庫（第 10 集：每個服務一個，foodgo_ordering、foodgo_payment…）。"""
    port = os.environ.get("FOODGO_POSTGRES_PORT", "15432")
    return f"postgresql://foodgo:foodgo@127.0.0.1:{port}/{database}"


def database_url(database: str = "foodgo") -> str:
    """這個程序用的資料庫：有設 FOODGO_DATABASE_URL 就用它，沒有就是 compose 那台的 database（預設 foodgo）。"""
    return os.environ.get("FOODGO_DATABASE_URL", postgres_url(database))


def kafka_bootstrap() -> str:
    port = os.environ.get("FOODGO_KAFKA_PORT", "19092")
    return os.environ.get("FOODGO_KAFKA_BOOTSTRAP", f"127.0.0.1:{port}")


def saga_mode() -> str:
    """第 8 集：付款、廚房、外送的 consumer 要聽誰。orchestration（預設）＝聽編排器的命令；choreography＝第 7 集的接力。"""
    mode = os.environ.get("FOODGO_SAGA", "orchestration")
    if mode not in ("orchestration", "choreography"):
        raise ValueError(f"FOODGO_SAGA 只能是 orchestration 或 choreography：{mode!r}")
    return mode


# ---------------------------------------------------------------- 第 10 集：拆成服務
PORTS = {"gateway": 8000, "ordering": 8101, "payment": 8102, "kitchen": 8103, "delivery": 8104}  # 對外只有 gateway


def service_port(name: str) -> int:
    """這個服務聽哪個埠：FOODGO_<名字>_PORT，例如 FOODGO_KITCHEN_PORT=8103。"""
    return int(os.environ.get(f"FOODGO_{name.upper()}_PORT", PORTS[name]))


def service_url(name: str) -> str:
    """Gateway 去哪裡找這個服務：FOODGO_<名字>_URL（第 11 集在容器裡會是 http://kitchen:8000 這種）。"""
    return os.environ.get(f"FOODGO_{name.upper()}_URL", f"http://127.0.0.1:{service_port(name)}")


def service_host() -> str:
    """服務綁在哪個位址：本機練習只給自己連（127.0.0.1）；容器裡要設 FOODGO_HOST=0.0.0.0。"""
    return os.environ.get("FOODGO_HOST", "127.0.0.1")


def otlp_endpoint() -> str:
    """span 送到哪（OTLP／HTTP）：OpenTelemetry 的標準變數 OTEL_EXPORTER_OTLP_ENDPOINT，沒有就是 compose 的 Jaeger。"""
    port = os.environ.get("FOODGO_OTLP_PORT", "14318")
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{port}").rstrip("/")


def jaeger_url() -> str:
    """Jaeger UI（看 trace 的網頁）。"""
    port = os.environ.get("FOODGO_JAEGER_UI_PORT", "16686")
    return os.environ.get("FOODGO_JAEGER_URL", f"http://127.0.0.1:{port}")
