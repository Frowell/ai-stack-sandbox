"""ILLUSTRATIVE — spec for spec 06, would change app/gateway.py. Not wired in.

The usage-capture seam (README §4, design.md §1b). Today app/gateway.py's chat()
returns `resp.choices[0].message.content or ""` and DISCARDS `resp`, so token
usage is unreachable to any caller. A wrapper around chat() therefore cannot
recover tokens — the seam must live inside chat()'s body.

This adds an opt-in, backward-compatible return:
  - chat(messages)                       -> str            (UNCHANGED default; hot path)
  - chat(messages, return_usage=True)    -> (str, Usage)   (eval path opts in)

No existing caller is modified: app/agent.py and app/evals.py's judge call both
use the default string return. Only the eval served-model call opts in.

The exact form (return_usage flag here vs a chat_with_usage() sibling) is co-owned
with PR #1 so eval cost-capture and any PR#1 cost reporting share one helper.  # PR#1

Do NOT import from this file. Port the change into app/gateway.py.
"""
from __future__ import annotations

from openai.types import CompletionUsage

# In the real module these already exist:
#   from openai import OpenAI
#   from .config import settings
#   _client = OpenAI(base_url=settings.gateway_base_url, api_key=settings.gateway_api_key)
from .config import settings  # noqa: F401  (illustrative import path)
from .gateway import _client  # noqa: F401  (illustrative; same module in reality)


def chat(messages: list[dict], *, return_usage: bool = False, **kwargs):
    """Hot-path chat. Default return type is `str` (UNCHANGED).

    With return_usage=True, returns (content, usage) where `usage` is the OpenAI
    CompletionUsage (prompt_tokens / completion_tokens). If the gateway omits usage
    (e.g. a streamed response), usage is a zero-filled CompletionUsage and the
    caller is expected to mark the case usage_estimated=true — never crash.

    NOTE the `**kwargs` already present in the real signature forwards sampling
    params (temperature, seed, n) straight to create(); the eval/baseline path
    relies on that — see example_agent.py for how they get here.
    """
    resp = _client.chat.completions.create(model=settings.chat_model, messages=messages, **kwargs)
    content = resp.choices[0].message.content or ""
    if not return_usage:
        return content  # default: identical to today's behaviour
    usage = resp.usage or CompletionUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    return content, usage
