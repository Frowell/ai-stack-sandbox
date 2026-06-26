"""The hot-path seam.

Every model call in the app goes through here, and this module points at the
gateway, not a provider. Swapping OpenAI for Claude is an edit to
gateway/litellm_config.yaml -- this file never changes. That decoupling is the
whole point of putting a gateway in the hot path.
"""
from openai import OpenAI

from .config import settings

_client = OpenAI(base_url=settings.gateway_base_url, api_key=settings.gateway_api_key)


def embed(texts: list[str]) -> list[list[float]]:
    resp = _client.embeddings.create(model=settings.embedding_model, input=texts)
    return [d.embedding for d in resp.data]


def chat(messages: list[dict], **kwargs) -> str:
    resp = _client.chat.completions.create(
        model=settings.chat_model, messages=messages, **kwargs
    )
    return resp.choices[0].message.content or ""
