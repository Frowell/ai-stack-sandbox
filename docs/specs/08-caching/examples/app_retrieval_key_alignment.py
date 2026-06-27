"""ILLUSTRATIVE — a spec, not wired-in code.

Key-strategy ALIGNMENT for app/retrieval.py's embedding cache (a NON-GOAL to
rewrite; in scope only to align keys so the gateway cache and the retrieval cache
don't tread on each other in the same Redis DB).

The current code:

    def _cached_embed(text: str) -> list[float]:
        key = f"emb:{hash(text)}"          # <-- builtin hash() is per-process salted
        ...

Python's builtin hash() for str is randomized per process (PYTHONHASHSEED), so:
  - keys do NOT survive a restart (cache is effectively non-persistent), and
  - keys never collide across processes (so two app processes never share entries).
i.e. it barely caches today. The alignment is a STABLE digest + a shared key-prefix
convention, so the gateway response cache (keyed by LiteLLM) and this embedding
cache live side by side without collision.

This is the only change in scope for retrieval.py — not a rewrite of the cache.
"""
from __future__ import annotations

import hashlib

# Shared prefix convention so namespaces are legible in a single Redis DB:
#   emb:   -> retrieval-side query-embedding cache (this file)
#   litellm:* / <namespace> -> gateway response cache (managed by LiteLLM)
_EMB_PREFIX = "emb"


def _stable_emb_key(text: str) -> str:
    """sha256 instead of builtin hash(): stable across processes and restarts."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_EMB_PREFIX}:{digest}"


# Drop-in for the one line in app/retrieval.py:_cached_embed:
#     key = _stable_emb_key(text)        # was: key = f"emb:{hash(text)}"
#
# Everything else in _cached_embed (the try/except fail-open, the 3600s setex)
# stays exactly as-is — this is alignment, not a rewrite (README non-goals).
