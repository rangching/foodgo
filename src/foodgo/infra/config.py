"""連線設定：預設對到 compose.yaml 的埠號；要改就設環境變數（或 .env＋uv run --env-file .env）。"""
import os


def database_url() -> str:
    port = os.environ.get("FOODGO_POSTGRES_PORT", "15432")
    return os.environ.get("FOODGO_DATABASE_URL", f"postgresql://foodgo:foodgo@127.0.0.1:{port}/foodgo")


def kafka_bootstrap() -> str:
    port = os.environ.get("FOODGO_KAFKA_PORT", "19092")
    return os.environ.get("FOODGO_KAFKA_BOOTSTRAP", f"127.0.0.1:{port}")


def saga_mode() -> str:
    """第 8 集：付款、廚房、外送的 consumer 要聽誰。orchestration（預設）＝聽編排器的命令；choreography＝第 7 集的接力。"""
    mode = os.environ.get("FOODGO_SAGA", "orchestration")
    if mode not in ("orchestration", "choreography"):
        raise ValueError(f"FOODGO_SAGA 只能是 orchestration 或 choreography：{mode!r}")
    return mode
