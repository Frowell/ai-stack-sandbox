"""ILLUSTRATIVE — a spec, not wired-in code.

Shows how the cache metadata from chat_with_meta() (examples/app_gateway.py) is
attached to the existing `generate` span. The real app/observability.py already
exposes a `span(name, **attributes)` context manager that sets any non-None
attribute — so cache visibility is just a few more attributes on the span that
already wraps the model call in app/agent.py:generate_node.

This satisfies the acceptance criterion: cache hit/miss, layer, and estimated
savings visible as OTEL span attributes (not just stdout).
"""
from __future__ import annotations

from .gateway import CacheMeta  # examples/app_gateway.py (illustrative)
from .observability import span


def cache_span_attributes(meta: CacheMeta) -> dict:
    """Map CacheMeta -> OTEL attribute keys. Kept as a helper so the same mapping
    is used everywhere a cached call is traced."""
    return {
        "cache.hit": meta.hit,
        "cache.layer": meta.layer,             # prompt | exact | semantic | miss
        "cache.tokens_saved": meta.tokens_saved,
        "cache.cost_saved_usd": meta.cost_saved_usd,
    }


# How app/agent.py:generate_node would use it (illustrative). The real node opens
# a "generate" span and calls chat(); it would call chat_with_meta() and merge in
# the cache attributes:
#
#   from .gateway import chat_with_meta
#   from .observability import span, cache_span_attributes
#
#   def generate_node(state):
#       ctx = "\n\n".join(f"[{i}] {c}" for i, c in state["context"])
#       messages = [
#           {"role": "system", "content": "Answer using only the provided context. ..."},
#           {"role": "user",   "content": f"Context:\n{ctx}\n\nQuestion: {state['question']}"},
#       ]
#       with span("generate", **{"gen_ai.operation.name": "chat",
#                                "gen_ai.request.model": "chat"}) as s:
#           answer, meta = chat_with_meta(messages)
#           for k, v in cache_span_attributes(meta).items():
#               s.set_attribute(k, v)
#       return {"answer": answer}
