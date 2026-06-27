"""ILLUSTRATIVE — spec for the additive part of app/gateway.py, not wired in.

The existing seam wraps an OpenAI client; /rerank is NOT an OpenAI SDK route, so
we add a thin httpx POST to {gateway_base_url}/rerank with the gateway master
key. httpx is already present transitively via `openai` — do NOT add `requests`.

Only the NEW `rerank()` is shown plus the existing imports it relies on; embed()
and chat() are unchanged.
"""
import httpx
from openai import OpenAI

from .config import settings

_client = OpenAI(base_url=settings.gateway_base_url, api_key=settings.gateway_api_key)

# Pin ONE path. LiteLLM serves both /rerank and /v1/rerank; pinning + asserting it
# in the contract test stops a gateway upgrade from silently 404-ing into fail-open.
_RERANK_PATH = "/rerank"


def rerank(
    query: str,
    documents: list[str],
    *,
    model: str,
    top_n: int,
    timeout: float,
) -> list[tuple[int, float]]:
    """Score `documents` against `query` through the gateway's rerank route.

    Returns [(index_into_documents, relevance_score), ...] in the provider's
    returned order. The CALLER maps `index` back to candidate ids — this function
    never sees DB ids, mirroring how embed()/chat() stay id-agnostic.

    Raises on transport error, timeout, non-2xx, or a malformed body; the
    retrieval-side fail-open wrapper turns any raise into RRF order.
    """
    resp = httpx.post(
        f"{settings.gateway_base_url}{_RERANK_PATH}",
        headers={"Authorization": f"Bearer {settings.gateway_api_key}"},
        json={"model": model, "query": query, "documents": documents, "top_n": top_n},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    # LiteLLM normalizes Cohere/Voyage to {"results":[{"index","relevance_score"},...]}.
    return [(r["index"], float(r["relevance_score"])) for r in body["results"]]
