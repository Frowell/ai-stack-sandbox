"""ILLUSTRATIVE — a spec, not wired-in code.

A tiny per-model price table so cache.tokens_saved can be turned into an estimated
dollar figure (cache.cost_saved_usd). The README calls for "estimated $ savings
from a per-model price table" — this is that table, kept deliberately small.

Prices are USD per 1M tokens and are illustrative placeholders; the real table
should be sourced from the provider's pricing page and kept in one place.
Savings semantics differ by layer (design.md §6):
  - exact/semantic HIT: the whole COMPLETION was served from cache -> save BOTH the
    input cost AND the output cost. For chat models the output rate is usually the
    LARGER of the two, so an input-only table understates savings on the layer that
    saves the most. That is why cost_saved_usd takes prompt_tokens AND
    completion_tokens for a response-cache hit.
  - prompt-cache discount: only the cached input prefix is discounted, and not to
    zero (provider charges a reduced rate for cached input, not nothing) -> save the
    DELTA between full and cached input rate on cached_tokens; NO output saving (the
    model still generated the completion).
"""
from __future__ import annotations

# USD per 1,000,000 tokens (illustrative). Input and output rates differ — output is
# typically several times the input rate, which is exactly why a response-cache hit's
# savings are dominated by the avoided OUTPUT cost.
_INPUT_USD_PER_M: dict[str, float] = {
    "openai/gpt-4o-mini": 0.15,
    "openai/text-embedding-3-small": 0.02,
}
_OUTPUT_USD_PER_M: dict[str, float] = {
    "openai/gpt-4o-mini": 0.60,            # ~4x the input rate (illustrative)
    "openai/text-embedding-3-small": 0.0,  # embeddings have no completion tokens
}

# Fraction of the input rate a provider still charges for a prompt-cache READ.
# OpenAI discounts cached input (it is not free). ~0.5 is a conservative placeholder;
# confirm against current provider pricing.
_PROMPT_CACHE_READ_FRACTION = 0.5


def _resolve_model(alias_or_model: str) -> str:
    # app/config.py uses the alias "chat"; map it to the underlying model for pricing.
    return {"chat": "openai/gpt-4o-mini", "embeddings": "openai/text-embedding-3-small"}.get(
        alias_or_model, alias_or_model
    )


def cost_saved_usd(
    model: str, prompt_tokens: int, completion_tokens: int, layer: str
) -> float:
    m = _resolve_model(model)
    in_rate = _INPUT_USD_PER_M.get(m, 0.0) / 1_000_000
    out_rate = _OUTPUT_USD_PER_M.get(m, 0.0) / 1_000_000
    if layer in ("exact", "semantic"):
        # full input AND output cost avoided (the stored completion is served)
        return round(prompt_tokens * in_rate + completion_tokens * out_rate, 6)
    if layer == "prompt":
        # only the cached input prefix is discounted; no output saving
        return round(prompt_tokens * in_rate * (1 - _PROMPT_CACHE_READ_FRACTION), 6)
    return 0.0
