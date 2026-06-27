"""ILLUSTRATIVE — spec for app/retrieval.py (NOT wired in).

Shows the one structural change: replace the `tuple[int, str]` carrier with a
`RetrievedChunk` NamedTuple and thread `meta` through every seam
(dense/sparse -> rrf -> rerank -> retrieve). Only the diffs from today's file are
meaningful; the redis/embed helpers (`_cached_embed`, `_vec_literal`, `span`,
`settings`) are unchanged and elided here for focus.

Carrier today:  tuple[int, str]
Carrier after:  RetrievedChunk(id, content, meta)   <- "no 2-tuple survives"
"""
from typing import NamedTuple

import psycopg

from .config import settings
from .observability import span
# from ._helpers import _cached_embed, _vec_literal   # unchanged, elided


class RetrievedChunk(NamedTuple):
    """The single carrier for a retrieved chunk across the whole pipeline.

    NamedTuple (not dataclass) so positional unpacking keeps working where we
    control both ends; `.id/.content/.meta` reads everywhere else. See design.md §1.
    `meta` is ALWAYS a dict by the time it leaves retrieval — the SQL boundary
    coerces NULL -> {} (design.md §2).
    """
    id: int
    content: str
    meta: dict


def dense(conn, query: str, k: int) -> list[RetrievedChunk]:
    v = _vec_literal(_cached_embed(query))  # noqa: F821 (elided helper)
    rows = conn.execute(
        # + meta in the projection.                              # DEPENDS-ON-#2
        "SELECT id, content, meta FROM documents "
        "ORDER BY embedding <=> %s::vector LIMIT %s",
        (v, k),
    ).fetchall()
    # r[2] or {} : psycopg3 decodes jsonb NULL -> Python None, not {} (design.md §2).
    return [RetrievedChunk(r[0], r[1], r[2] or {}) for r in rows]


def sparse(conn, query: str, k: int) -> list[RetrievedChunk]:
    rows = conn.execute(
        "SELECT id, content, meta FROM documents "                # DEPENDS-ON-#2
        "WHERE fts @@ plainto_tsquery('english', %s) "
        "ORDER BY ts_rank(fts, plainto_tsquery('english', %s)) DESC LIMIT %s",
        (query, query, k),
    ).fetchall()
    return [RetrievedChunk(r[0], r[1], r[2] or {}) for r in rows]


def rrf(*ranked_lists, k: int = 60) -> list[RetrievedChunk]:
    """Reciprocal Rank Fusion: combine ranked lists without tuning score scales.

    Adds a parallel `meta` map alongside the existing `text` map. dense and sparse
    return the same meta for a given id, so last-writer-wins is fine.
    """
    scores: dict[int, float] = {}
    text: dict[int, str] = {}
    meta: dict[int, dict] = {}
    for lst in ranked_lists:
        for rank, c in enumerate(lst):            # c is a RetrievedChunk now
            scores[c.id] = scores.get(c.id, 0.0) + 1.0 / (k + rank + 1)
            text[c.id] = c.content
            meta[c.id] = c.meta
    ordered = sorted(scores, key=lambda i: scores[i], reverse=True)
    return [RetrievedChunk(i, text[i], meta[i]) for i in ordered]


def rerank(
    query: str, candidates: list[RetrievedChunk], top_n: int
) -> list[RetrievedChunk]:
    # Identity (RRF order), unchanged behavior — now passes RetrievedChunk through.
    # Real reranking is spec 04's job, not this one.
    return candidates[:top_n]


def retrieve(query: str, top_n: int = 4, pool: int = 10) -> list[RetrievedChunk]:
    with span("retrieve", **{"gen_ai.operation.name": "retrieve", "retrieval.query": query}):
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            fused = rrf(dense(conn, query, pool), sparse(conn, query, pool))
            return rerank(query, fused, top_n)
