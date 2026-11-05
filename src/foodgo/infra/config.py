"""連線設定：預設對到 compose.yaml 的埠號；要改就設環境變數（或 .env＋uv run --env-file .env）。"""
import os


def database_url() -> str:
    port = os.environ.get("FOODGO_POSTGRES_PORT", "15432")
    return os.environ.get("FOODGO_DATABASE_URL", f"postgresql://foodgo:foodgo@127.0.0.1:{port}/foodgo")


def kafka_bootstrap() -> str:
    port = os.environ.get("FOODGO_KAFKA_PORT", "19092")
    return os.environ.get("FOODGO_KAFKA_BOOTSTRAP", f"127.0.0.1:{port}")
