"""ILLUSTRATIVE — proposed additive change to app/gateway.py. Not wired in.

The existing `chat(messages, **kwargs) -> str` is UNCHANGED and keeps all three of
its current callers (app/agent.py generate_node, app/evals.py judge, and the
supervisor's optional LLM route) working. We only ADD a sibling that also returns
token usage, because `token_budget` enforcement needs `resp.usage` which today's
`chat()` discards.
"""
from dataclasses import dataclass

from openai import OpenAI

from .config import settings

_client = OpenAI(base_url=settings.gateway_base_url, api_key=settings.gateway_api_key)


@dataclass(frozen=True)
class ChatResult:
    text: str
    total_tokens: int  # 0 when the provider/path omits usage (see README open question)


def chat_with_usage(messages: list[dict], **kwargs) -> ChatResult:
    resp = _client.chat.completions.create(
        model=settings.chat_model, messages=messages, **kwargs
    )
    usage = getattr(resp, "usage", None)
    total = getattr(usage, "total_tokens", 0) or 0
    return ChatResult(text=resp.choices[0].message.content or "", total_tokens=total)


# Existing function stays exactly as-is:
# def chat(messages: list[dict], **kwargs) -> str:
#     resp = _client.chat.completions.create(model=settings.chat_model, messages=messages, **kwargs)
#     return resp.choices[0].message.content or ""
