"""ILLUSTRATIVE — proposed change to `app/gateway.py` for spec 17. Not wired in.

The ONLY change 17 needs here is the success-signal seam (design.md §2), and it is
SHARED with spec 01: decide it together at integration. 17 forbids exactly one
thing -- inferring success from emptiness (`content or ""`), which collapses
"model returned empty" with "the call failed".

Default choice (Option A): chat() RAISES on transport failure (5xx / timeout /
fallback-chain exhausted -- the latter handled by spec 01's router), and returns the
bare string otherwise. Then any string that reaches generate_node is, by
construction, a SUCCESSFUL completion -- so an empty string there is an
empty-success, not a transport-empty. app.safety.handle() relies on this.

chat() stays a thin transport. NO refusal handling here (that belongs one layer up,
in generate_node -- design.md §3). embed() is unchanged.
"""
from openai import APIError, OpenAI

from .config import settings

_client = OpenAI(base_url=settings.gateway_base_url, api_key=settings.gateway_api_key)


def embed(texts: list[str]) -> list[list[float]]:
    resp = _client.embeddings.create(model=settings.embedding_model, input=texts)
    return [d.embedding for d in resp.data]


def chat(messages: list[dict], *, model: str | None = None, **kwargs) -> str:
    # TWO changes 17 needs here (both small, both coordinated with spec 01/06):
    #
    # 1. Explicit `model` override. The CURRENT chat() hardcodes
    #    `model=settings.chat_model` and then splats **kwargs, so a caller passing
    #    `model="judge"` (the pinned safety judge, AC7) hits TypeError: "multiple
    #    values for keyword argument 'model'". Lifting `model` to a named param fixes
    #    that and leaves prod callers (no `model=`) on settings.chat_model unchanged.
    #
    # 2. Success-signal seam (Option A). BEFORE (today):
    #        return resp.choices[0].message.content or ""
    #    -> empty-success and transport-empty are indistinguishable. 17 cannot build
    #    its refusal seam on this. AFTER (Option A): let transport errors propagate
    #    (spec 01 owns the fallback chain + retries; once exhausted it raises). On a
    #    successful completion, return the raw content -- which MAY legitimately be the
    #    empty string.
    resp = _client.chat.completions.create(
        model=model or settings.chat_model, messages=messages, **kwargs
    )
    return resp.choices[0].message.content or ""
    # NOTE: with Option A the `or ""` is now SAFE: we only reach this line on a
    # successful call, so "" unambiguously means empty-success. The transport-empty
    # case never reaches here because APIError propagated.
    #
    # If spec 01 instead picks Option B (return a ChatResult(content, ok, model,...)),
    # app.safety.handle() inspects result.ok instead of relying on "a string == success".
    # Either is acceptable; agree it at spec 01 integration.


# Reference only -- APIError imported above to make explicit that 17 expects chat()
# to surface transport failures as exceptions (Option A), not as "".
_ = APIError
