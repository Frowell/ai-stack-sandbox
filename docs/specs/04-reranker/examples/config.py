"""ILLUSTRATIVE — spec for app/config.py, not wired in.

Adds the rerank fields to the existing frozen Settings dataclass, following the
established convention: a lowercase attribute populated from an UPPER_SNAKE env
var (e.g. chat_model <- CHAT_MODEL). Only the *new* lines are shown in context;
the rest of Settings is unchanged.
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
    gateway_api_key: str = os.environ.get("LITELLM_MASTER_KEY", "sk-sandbox-master")
    chat_model: str = os.environ.get("CHAT_MODEL", "chat")
    embedding_model: str = os.environ.get("EMBEDDING_MODEL", "embeddings")
    embedding_dim: int = int(os.environ.get("EMBEDDING_DIM", "1536"))

    # --- NEW: reranker (all opt-in; default `none` reproduces today's identity) ---
    # none (default) | local | cohere | voyage
    rerank_backend: str = os.environ.get("RERANK_BACKEND", "none")
    # Overloaded by backend (like chat_model/embedding_model default to an alias):
    #   hosted -> the gateway ALIAS posted as `model` (default `rerank`, matching
    #             model_name: rerank in litellm_config.yaml). The provider id
    #             (cohere/rerank-english-v3.0) lives ONLY in litellm_config.yaml;
    #             posting it here would 400 into permanent fail-open. Hosted reads
    #             `rerank_model or "rerank"`, so empty is fine for hosted.
    #   local  -> the actual cross-encoder id, e.g.
    #             cross-encoder/ms-marco-MiniLM-L-6-v2 (no gateway; empty -> fail-open).
    # Unused for `none`.
    rerank_model: str = os.environ.get("RERANK_MODEL", "")
    # how many of the FUSED candidates to actually score (fused[:rerank_pool]).
    # Independent of retrieve()'s `pool` arg. Bounds local CPU latency.
    rerank_pool: int = int(os.environ.get("RERANK_POOL", "10"))
    # hosted call budget (seconds), passed as the httpx request timeout. Bounds
    # ONLY the network backend; the synchronous local backend is bounded by pool.
    rerank_timeout_s: float = float(os.environ.get("RERANK_TIMEOUT_S", "5.0"))


settings = Settings()
