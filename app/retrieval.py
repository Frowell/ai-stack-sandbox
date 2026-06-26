"""Hybrid retrieval over pgvector.

Dense (vector cosine) + sparse (Postgres full-text), merged with Reciprocal Rank
Fusion, then a reranker hook. Redis caches query embeddings; if Redis is down the
cache simply no-ops. This is the same shape as a Pinecone-hybrid + Cohere-rerank
pipeline, just self-contained in one Postgres container.
"""
import json

import psycopg

from .config import settings
from .gateway import embed
from .observability import span

try:
    import redis

    _redis = redis.from_url(settings.redis_url)
    _redis.ping()
except Exception:
    _redis = None  # cache is optional


def _cached_embed(text: str) -> list[float]:
    key = f"emb:{hash(text)}"
    if _redis:
        try:
            hit = _redis.get(key)
            if hit:
                return json.loads(hit)
        except Exception:
            pass
    vec = embed([text])[0]
    if _redis:
        try:
            _redis.setex(key, 3600, json.dumps(vec))
        except Exception:
            pass
    return vec


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in v) + "]"


def dense(conn, query: str, k: int) -> list[tuple[int, str]]:
    v = _vec_literal(_cached_embed(query))
    rows = conn.execute(
        "SELECT id, content FROM documents ORDER BY embedding <=> %s::vector LIMIT %s",
        (v, k),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def sparse(conn, query: str, k: int) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT id, content FROM documents "
        "WHERE fts @@ plainto_tsquery('english', %s) "
        "ORDER BY ts_rank(fts, plainto_tsquery('english', %s)) DESC LIMIT %s",
        (query, query, k),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def rrf(*ranked_lists, k: int = 60) -> list[tuple[int, str]]:
    """Reciprocal Rank Fusion: combine ranked lists without tuning score scales."""
    scores: dict[int, float] = {}
    text: dict[int, str] = {}
    for lst in ranked_lists:
        for rank, (doc_id, content) in enumerate(lst):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
            text[doc_id] = content
    ordered = sorted(scores, key=lambda i: scores[i], reverse=True)
    return [(i, text[i]) for i in ordered]


def rerank(query: str, candidates: list[tuple[int, str]], top_n: int) -> list[tuple[int, str]]:
    # Plug a cross-encoder or Cohere/Voyage rerank here. Default = identity (RRF order).
    return candidates[:top_n]


def retrieve(query: str, top_n: int = 4, pool: int = 10) -> list[tuple[int, str]]:
    with span("retrieve", **{"gen_ai.operation.name": "retrieve", "retrieval.query": query}):
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            fused = rrf(dense(conn, query, pool), sparse(conn, query, pool))
            return rerank(query, fused, top_n)
