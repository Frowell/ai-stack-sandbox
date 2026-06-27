"""ILLUSTRATIVE target for app/gateway.py — a spec, not wired-in code.

Shows the one app-side change: capture the *served* deployment and record it on
the active OTel span, WITHOUT changing chat()'s signature (so app/agent.py and
app/evals.py keep calling `chat(messages) -> str` unchanged).

Why on the span and not in the return value: changing the return type would
ripple through both call sites for an observability-only need. The `generate`
node in app/agent.py already opens a span around the chat() call, so chat() can
just annotate the current span. See ../design.md "Interface sketch".
"""
from openai import OpenAI
from opentelemetry import trace

from .config import settings

_client = OpenAI(base_url=settings.gateway_base_url, api_key=settings.gateway_api_key)


def embed(texts: list[str]) -> list[list[float]]:
    resp = _client.embeddings.create(model=settings.embedding_model, input=texts)
    return [d.embedding for d in resp.data]


def chat(messages: list[dict], **kwargs) -> str:
    # `.with_raw_response` exposes the HTTP response so we can read the
    # OSS-guaranteed `x-litellm-model-id` header (the authoritative served
    # deployment). `.parse()` then gives the normal typed ChatCompletion.
    raw = _client.chat.completions.with_raw_response.create(
        model=settings.chat_model, messages=messages, **kwargs
    )
    served = raw.headers.get("x-litellm-model-id")  # e.g. "chat-bedrock"
    resp = raw.parse()

    # Record the served deployment on whatever span is active (the `generate`
    # node opens one). `gen_ai.request.model` stays "chat" (the requested alias);
    # `gen_ai.response.model` is the new fact: which deployment actually answered.
    # get_current_span() never returns None — with no active span it returns a
    # non-recording INVALID_SPAN, so guard on is_recording() (set_attribute on a
    # non-recording span is a silent no-op anyway, but this is clearer).
    current = trace.get_current_span()
    if current.is_recording():
        current.set_attribute("gen_ai.response.model", served or resp.model)

    return resp.choices[0].message.content or ""
