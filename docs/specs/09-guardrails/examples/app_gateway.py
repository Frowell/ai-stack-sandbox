"""ILLUSTRATIVE — the changed app/gateway.py.

The seam still names no scanner. Two additions:
  1. read guardrail decision headers via the OpenAI SDK's with_raw_response form
  2. map a gateway guardrail BLOCK (HTTP 400) to the GuardrailBlocked sentinel

Today's signature `chat(messages, **kwargs) -> str` is preserved as the default;
callers that want the decision pass with_decision=True (agent.py does).
"""
from openai import BadRequestError, OpenAI

from .config import settings
from .guardrails import (  # re-exported so agent.py can `from .gateway import GuardrailBlocked`
    GuardrailBlocked,
    GuardrailBlockedError,
    GuardrailDecision,
    block_from_error,
    decision_from_headers,
)

__all__ = ["chat", "embed", "GuardrailBlocked", "GuardrailBlockedError", "GuardrailDecision"]

_client = OpenAI(base_url=settings.gateway_base_url, api_key=settings.gateway_api_key)


def embed(texts: list[str]) -> list[list[float]]:
    # PII redaction happens inside the gateway pre_call hook, so the provider
    # receives redacted inputs without this code naming a scanner. The ONE new
    # behavior: a fail-closed guardrail block (HTTP 400) becomes a typed
    # GuardrailBlockedError instead of a bare openai.BadRequestError, so callers
    # (retrieval._cached_embed, ingest) can handle it explicitly (README AC3).
    try:
        resp = _client.embeddings.create(model=settings.embedding_model, input=texts)
    except BadRequestError as err:
        blocked = block_from_error(err)
        if blocked is not None:
            raise GuardrailBlockedError(blocked.reason, blocked.guardrail) from err
        raise  # non-guardrail 400 -> unchanged behavior
    return [d.embedding for d in resp.data]


def chat(messages: list[dict], *, with_decision: bool = False, **kwargs):
    """Return str (default) or (str, GuardrailDecision) when with_decision=True.

    Raises GuardrailBlocked when the gateway blocks the call; re-raises any other
    BadRequestError unchanged.
    """
    try:
        raw = _client.chat.completions.with_raw_response.create(  # VERIFY: .with_raw_response API
            model=settings.chat_model, messages=messages, **kwargs
        )
    except BadRequestError as err:
        blocked = block_from_error(err)
        if blocked is not None:
            raise blocked from err      # generate_node catches this and refuses safely
        raise                           # non-guardrail 400 -> unchanged behavior

    resp = raw.parse()
    text = resp.choices[0].message.content or ""
    if not with_decision:
        return text
    return text, decision_from_headers(raw.headers)
