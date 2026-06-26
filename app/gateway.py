"""The hot-path seam.

Every model call in the app goes through here, and this module points at the
gateway, not a provider. Swapping OpenAI for Claude is an edit to
gateway/litellm_config.yaml -- this file never changes. That decoupling is the
whole point of putting a gateway in the hot path.
"""
import contextvars

from openai import OpenAI

from .config import settings

_client = OpenAI(base_url=settings.gateway_base_url, api_key=settings.gateway_api_key)

# The model that actually served the most recent chat() call on this context.
# LiteLLM echoes the resolved deployment in resp.model, so reading it back catches
# silent alias drift or a fallback (e.g. Anthropic -> Bedrock) activating mid-run.
# Note: the response does NOT echo temperature -- drop_params can strip it
# invisibly -- which is why the eval samples each case N times rather than trusting
# a single greedy call.
_served_model: contextvars.ContextVar = contextvars.ContextVar("served_model", default=None)


def served_model():
    return _served_model.get()


def embed(texts: list[str]) -> list[list[float]]:
    resp = _client.embeddings.create(model=settings.embedding_model, input=texts)
    return [d.embedding for d in resp.data]


def chat(messages: list[dict], *, model: str | None = None, **kwargs) -> str:
    kwargs.setdefault("temperature", settings.temperature)  # greedy by default; eval == prod
    resp = _client.chat.completions.create(
        model=model or settings.chat_model, messages=messages, **kwargs
    )
    _served_model.set(getattr(resp, "model", None))
    return resp.choices[0].message.content or ""
