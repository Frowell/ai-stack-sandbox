"""ILLUSTRATIVE — spec for the rerank parts of app/retrieval.py, not wired in.

Shows the dispatcher, per-call backend selection, the fail-open wrapper, the
`span("rerank")` instrumentation, and the lazily-imported, cached local backend.
Everything ABOVE rerank() in the real file (dense/sparse/rrf/retrieve, the Redis
cache) is unchanged and omitted here except `retrieve()`, which only changes in
that its existing rerank(...) call now resolves a real backend.
"""
import logging

from . import gateway
from .config import settings  # module global; tests patch app.retrieval.settings
from .observability import span

log = logging.getLogger(__name__)

# Heavyweight resource cached once; only the *selection* below is per-call.
_local_model = None  # type: ignore[var-annotated]
_warned_fallback = False


def _warn_once(msg: str) -> None:
    """Make a misconfigured deployment visible without logging per query."""
    global _warned_fallback
    if not _warned_fallback:
        log.warning("rerank falling back to RRF order: %s", msg)
        _warned_fallback = True


def _rerank_local(
    query: str, candidates: list[tuple[int, str]], top_n: int
) -> list[tuple[int, str]]:
    """Cross-encoder backend. Lazy import so (a) selecting `local` without the
    `rerank-local` uv group degrades via fail-open instead of crashing import, and
    (b) torch/sentence-transformers load once, only when actually used."""
    global _local_model
    if _local_model is None:
        from sentence_transformers import CrossEncoder  # lazy; may raise ImportError

        _local_model = CrossEncoder(settings.rerank_model)
    pairs = [(query, content) for _, content in candidates]
    scores = _local_model.predict(pairs)  # CPU-bound; bounded by len(pairs)
    order = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)
    return [candidates[i] for i in order[:top_n]]


def _rerank_hosted(
    query: str, candidates: list[tuple[int, str]], top_n: int
) -> list[tuple[int, str]]:
    """Cohere/Voyage through the gateway. Map returned `index` back to candidate
    ids; an out-of-range index is a malformed body -> raises -> fail-open."""
    documents = [content for _, content in candidates]
    ranked = gateway.rerank(
        query,
        documents,
        # The gateway ALIAS, not a provider model id (mirrors chat/embeddings).
        # The provider id lives only in litellm_config.yaml; posting it here would
        # not match any model_name and would 400 into permanent silent fail-open.
        model=settings.rerank_model or "rerank",
        top_n=top_n,
        timeout=settings.rerank_timeout_s,
    )
    # Defensive slice: some providers ignore/cap top_n, so guarantee the same
    # result count as `none` (an out-of-range index still raises -> fail-open).
    return [candidates[idx] for idx, _score in ranked][:top_n]


def rerank(
    query: str, candidates: list[tuple[int, str]], top_n: int
) -> list[tuple[int, str]]:
    """Dispatch to settings.rerank_backend; fail-open to RRF order on ANY failure.

    Backend is read from the module-level `settings` PER CALL (not bound at
    import) so a `dataclasses.replace`-based patch of the frozen Settings takes
    effect in tests. The default `none` reproduces today's identity behaviour.
    """
    backend = settings.rerank_backend
    pool = candidates[: settings.rerank_pool]

    with span(
        "rerank",
        **{
            "rerank.backend": backend,
            "rerank.model": settings.rerank_model or None,  # span() drops None
            "rerank.candidates": len(candidates),
            "rerank.top_n": top_n,
        },
    ) as s:
        # Correctness guard, NOT a fall-back: 0/1 candidates is a no-op.
        if len(candidates) <= 1 or backend == "none":
            s.set_attribute("rerank.fell_back", False)
            return candidates[:top_n]
        try:
            if backend == "local":
                out = _rerank_local(query, pool, top_n)
            elif backend in ("cohere", "voyage"):
                out = _rerank_hosted(query, pool, top_n)
            else:
                raise ValueError(f"unknown RERANK_BACKEND={backend!r}")
            s.set_attribute("rerank.fell_back", False)
            # Hosted/usage cost, when LiteLLM returns it, would be set here as
            # float/int: s.set_attribute("gen_ai.usage.cost", cost)
            return out
        except Exception as exc:  # noqa: BLE001 — fail-open is the whole point
            _warn_once(f"backend={backend}: {exc!r}")
            s.set_attribute("rerank.fell_back", True)
            return candidates[:top_n]  # byte-for-byte today's identity behaviour


def retrieve(query: str, top_n: int = 4, pool: int = 10) -> list[tuple[int, str]]:
    """UNCHANGED control flow — the only difference is rerank() now has teeth."""
    import psycopg

    from .retrieval import dense, rrf, sparse  # illustrative; same module IRL

    with span("retrieve", **{"gen_ai.operation.name": "retrieve", "retrieval.query": query}):
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            fused = rrf(dense(conn, query, pool), sparse(conn, query, pool))
            return rerank(query, fused, top_n)
