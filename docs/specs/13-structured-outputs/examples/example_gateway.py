"""ILLUSTRATIVE — spec for spec 13, not wired-in code.

Shows the additions to `app/gateway.py`:
  - StructuredOutputError (typed failure carrying the raw text + cause)
  - _strict_schema (pydantic v2 schema -> OpenAI strict-mode schema)
  - chat_structured (sibling to chat(), one bounded retry, mandatory validation)

The existing module already has:  _client, chat(), embed(), settings.
Nothing here changes chat()'s `-> str` contract; this is a new sibling.

Do NOT import from this file. Port the pieces into app/gateway.py.
"""
from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, ValidationError

# In the real module these already exist:
# from openai import OpenAI
# from .config import settings
# _client = OpenAI(base_url=settings.gateway_base_url, api_key=settings.gateway_api_key)
from .config import settings  # noqa: F401  (illustrative import path)
from .gateway import _client  # noqa: F401  (illustrative; same module in reality)

T = TypeVar("T", bound=BaseModel)

# How many corrective re-asks before giving up. Bounded so a stubborn model can
# never hang CI. README open question: revisit only if live judges fail often.
_STRUCTURED_RETRIES = 1


class StructuredOutputError(Exception):
    """Raised when model output cannot be validated against the schema after the
    bounded retry. Carries enough to debug from a CI log alone: the schema that
    was violated, the final raw text, and the chained pydantic ValidationError.
    """

    def __init__(self, schema_name: str, raw: str, cause: ValidationError):
        self.schema_name = schema_name
        self.raw = raw
        super().__init__(f"output did not match schema {schema_name!r}: {cause}")


def _strict_schema(schema: type[BaseModel]) -> dict:
    """Turn a pydantic v2 schema into an OpenAI `strict: true`-compatible schema.

    OpenAI rejects (HTTP 400) any object that omits `additionalProperties: false`
    or leaves a property out of `required`. pydantic emits neither and adds a
    `title`. v1 scope: FLAT schemas only. If handed a nested schema ($defs),
    raise loudly rather than emit something the provider 400s on (see design.md §4).
    """
    js = schema.model_json_schema()
    if "$defs" in js or "$ref" in js:
        raise ValueError(
            f"{schema.__name__} has nested models ($defs/$ref); nested schemas are "
            "out of scope for structured-outputs v1 (spec 13, design.md §4)."
        )
    js.pop("title", None)
    for prop in js.get("properties", {}).values():
        prop.pop("title", None)
    js["additionalProperties"] = False
    js["required"] = list(js.get("properties", {}).keys())  # force ALL fields required
    return js


def chat_structured(messages: list[dict], schema: type[T], **kwargs) -> T:
    """Schema-constrained sibling of chat().

    Sends `response_format` json_schema (strict), then VALIDATES CLIENT-SIDE —
    which is authoritative. Even if the gateway drops the param (drop_params:true)
    and returns free text, model_validate_json raises and we retry/raise. The raw
    string is NEVER returned. One bounded corrective retry: the model's rejected
    reply is re-appended as an `assistant` turn, then a `user` corrective turn (NOT
    a 2nd system turn, and NOT a 2nd consecutive user turn — Anthropic, the
    advertised alias swap, rejects consecutive user turns). Note: on the failure
    path the underlying `create` is invoked twice — tests must account for this.
    """
    convo = list(messages)
    last_raw = ""
    last_err: ValidationError | None = None

    for attempt in range(_STRUCTURED_RETRIES + 1):
        resp = _client.chat.completions.create(
            model=settings.chat_model,
            messages=convo,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": _strict_schema(schema),
                    "strict": True,
                },
            },
            **kwargs,
        )
        last_raw = resp.choices[0].message.content or ""
        try:
            return schema.model_validate_json(last_raw)  # AUTHORITATIVE validation
        except ValidationError as e:
            last_err = e
            if attempt < _STRUCTURED_RETRIES:
                # Append the rejected reply as an assistant turn, THEN the corrective
                # user turn. Preserves user/assistant alternation (Anthropic rejects
                # consecutive user turns) and gives the model its mistake as context.
                # NOT a 2nd system turn — some providers honor only the first.
                convo = convo + [
                    # Empty-content guard: a refusal can return empty content, and
                    # Anthropic (the advertised alias swap) 400s on an empty content
                    # block — substitute a placeholder so the corrective turn stays
                    # valid under the same cross-provider mapping the alternation buys.
                    {"role": "assistant", "content": last_raw or "(empty response)"},
                    {"role": "user", "content": "Return JSON matching the schema, nothing else."},
                ]

    raise StructuredOutputError(schema.__name__, last_raw, last_err) from last_err
