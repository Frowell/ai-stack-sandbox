"""Single source of runtime config. In-container, hostnames come from compose env;
running from the host, the localhost defaults work because every service publishes
its port."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # pulls .env when run from the host; harmless in-container


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/sandbox"
    )
    redis_url: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    gateway_base_url: str = os.environ.get("GATEWAY_BASE_URL", "http://localhost:4000")
    gateway_api_key: str = os.environ.get("LITELLM_MASTER_KEY", "sk-sandbox-master")
    chat_model: str = os.environ.get("CHAT_MODEL", "chat")
    # Decoding params live in the hot path so eval runs the same config as prod
    # ("test what you ship"). 0 = greedy; drop_params in the gateway strips it on
    # tiers that reject it (Opus 4.8/4.7), so it is safe to leave set.
    temperature: float = float(os.environ.get("CHAT_TEMPERATURE", "0"))
    embedding_model: str = os.environ.get("EMBEDDING_MODEL", "embeddings")
    embedding_dim: int = int(os.environ.get("EMBEDDING_DIM", "1536"))


settings = Settings()
