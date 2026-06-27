"""ILLUSTRATIVE — a spec, not wired-in code. Mirrors app/config.py.

This is THE one application code change for this feature. Everything else is
gateway config, compose, and a seed script. app/gateway.py is UNCHANGED — it
already reads `settings.gateway_api_key`.

Only the `gateway_api_key` field changes; the rest of Settings is shown for
context. The new precedence is:

    LITELLM_VIRTUAL_KEY  (scoped, app runtime traffic)
      -> LITELLM_MASTER_KEY  (admin/seed; first-run fallback before seeding)
      -> "sk-sandbox-master" (the compose default, keeps the demo bootable)
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/sandbox"
    )
    redis_url: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    gateway_base_url: str = os.environ.get("GATEWAY_BASE_URL", "http://localhost:4000")

    # CHANGED. `or` (not a two-arg .get) so an empty LITELLM_VIRTUAL_KEY="" — set
    # but blank, e.g. before `make seed` runs — falls through to the master key
    # instead of authenticating with an empty string.
    gateway_api_key: str = (
        os.environ.get("LITELLM_VIRTUAL_KEY")
        or os.environ.get("LITELLM_MASTER_KEY", "sk-sandbox-master")
    )

    chat_model: str = os.environ.get("CHAT_MODEL", "chat")
    embedding_model: str = os.environ.get("EMBEDDING_MODEL", "embeddings")
    embedding_dim: int = int(os.environ.get("EMBEDDING_DIM", "1536"))


settings = Settings()
