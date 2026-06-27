"""ILLUSTRATIVE — a spec, not wired-in code.

Shows how app/gateway.py:chat() would be WIDENED to surface the cache metadata it
currently discards. The real chat() returns only resp.choices[0].message.content
and throws away the response object — which is exactly where LiteLLM puts the
cache-hit flag, the cached-token count, and the cache key (design.md §6).

Two compatible shapes are shown:
  1. chat() keeps its string return for existing callers (agent, evals), and a
     sibling chat_with_meta() exposes the metadata for the span-emitting path.
  2. a CacheMeta dataclass + a derive_cache_meta() helper that turns the raw SDK
     response into the cache.* attributes app/observability.py will attach.

Do NOT wire this in. It is the interface sketch for the acceptance criterion
"cache hit/miss + layer + savings visible as span attributes".
"""
from __future__ import annotations

from dataclasses import dataclass

from openai import OpenAI

from .config import settings
from .pricing import cost_saved_usd  # examples/app_pricing.py (illustrative)

_client = OpenAI(base_url=settings.gateway_base_url, api_key=settings.gateway_api_key)


def _truthy(header_value) -> bool:
    """Headers are strings; treat "true"/"1"/"yes" as True, missing/"false" as False."""
    return str(header_value).strip().lower() in {"true", "1", "yes"}


@dataclass(frozen=True)
class CacheMeta:
    hit: bool                 # served from a LiteLLM response cache (exact/semantic)
    layer: str                # "exact" | "semantic" | "prompt" | "miss"
    tokens_saved: int         # cached_tokens (prompt) or full prompt size (exact/semantic hit)
    cost_saved_usd: float


# --- existing callers keep the string contract -------------------------------
def chat(messages: list[dict], **kwargs) -> str:
    """Unchanged signature for the agent graph and the eval judge."""
    text, _meta = chat_with_meta(messages, **kwargs)
    return text


# --- the span-emitting path uses the metadata-bearing variant ----------------
def chat_with_meta(messages: list[dict], **kwargs) -> tuple[str, CacheMeta]:
    """Same call, but returns (text, CacheMeta).

    Uses the OpenAI SDK's `.with_raw_response` so BOTH the parsed body and the
    response headers are available — LiteLLM stamps cache status onto both.
    """
    raw = _client.chat.completions.with_raw_response.create(
        model=settings.chat_model, messages=messages, **kwargs
    )
    resp = raw.parse()
    meta = derive_cache_meta(resp, raw.headers)
    return resp.choices[0].message.content or "", meta


def derive_cache_meta(resp, headers) -> CacheMeta:
    """Turn a raw gateway response into cache.* attributes (design.md §6).

    Layer derivation:
      - a LiteLLM response-cache hit  -> "exact" | "semantic" (by configured type)
      - else cached_tokens > 0        -> "prompt" (live call, input-token discount)
      - else                          -> "miss"
    NOTE: over the OpenAI SDK the cache signal comes from the RESPONSE HEADERS, not
    `_hidden_params` — that attribute exists only on LiteLLM's own SDK responses, so
    via the OpenAI SDK `getattr(resp, "_hidden_params", {})` is always {} (kept only
    as a harmless fallback). The exact hit HEADER is VERSION-SENSITIVE: prefer an
    explicit boolean like `x-litellm-cache-hit` if the pinned build emits one;
    keying on the mere presence of `x-litellm-cache-key` can FALSE-POSITIVE if that
    header is also set on misses. Confirm against the pinned image (design.md §6).
    """
    hidden = getattr(resp, "_hidden_params", {}) or {}   # {} via the OpenAI SDK; see note
    response_cache_hit = bool(
        hidden.get("cache_hit")
        or _truthy(headers.get("x-litellm-cache-hit"))   # preferred explicit boolean header
        or headers.get("x-litellm-cache-key")            # fallback; confirm it is hit-only
    )

    # prompt-cache discount on an otherwise-live call (OpenAI: cached_tokens)
    details = getattr(resp.usage, "prompt_tokens_details", None)
    cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
    prompt_tokens = int(getattr(resp.usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(resp.usage, "completion_tokens", 0) or 0)

    if response_cache_hit:
        layer = settings.cache_layer_name      # "exact" or "semantic", from config
        # a response-cache hit avoids BOTH input and output cost (design.md §6);
        # tokens_saved reports the total served-from-cache token count.
        tokens_saved = prompt_tokens + completion_tokens
        cost_saved = cost_saved_usd(settings.chat_model, prompt_tokens, completion_tokens, layer)
    elif cached_tokens > 0:
        layer = "prompt"
        tokens_saved = cached_tokens
        # prompt cache discounts only the cached input prefix; pass it as prompt_tokens
        cost_saved = cost_saved_usd(settings.chat_model, cached_tokens, 0, layer)
    else:
        layer = "miss"
        tokens_saved = 0
        cost_saved = 0.0

    return CacheMeta(
        hit=response_cache_hit,
        layer=layer,
        tokens_saved=tokens_saved,
        cost_saved_usd=cost_saved,
    )
