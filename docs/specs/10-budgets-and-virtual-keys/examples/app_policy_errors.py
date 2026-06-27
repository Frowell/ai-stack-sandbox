"""ILLUSTRATIVE — a spec, not wired-in code. Mirrors a NEW (optional) file:
app/policy_errors.py.

The README states the error contract (point 5): the OpenAI SDK in app/gateway.py
surfaces gateway policy denials as typed exceptions:

    over budget   -> openai.BadRequestError      (HTTP 400, budget reason in body)
    rate limited  -> openai.RateLimitError       (HTTP 429)
    revoked/bad   -> openai.AuthenticationError   (HTTP 401)

This helper lets callers (and the unit test) distinguish "denied by policy" from a
genuine provider/model error WITHOUT app/gateway.py changing — it inspects the
exception the SDK already raises. It is a thin classifier, not a new control path.

`classify_gateway_error` is the seam the unit test exercises (testing.md, AC:
"app distinguishes policy-denial from provider errors").
"""
from __future__ import annotations

from enum import Enum

import openai


class PolicyDenial(str, Enum):
    OVER_BUDGET = "over_budget"     # 400
    RATE_LIMITED = "rate_limited"   # 429
    UNAUTHORIZED = "unauthorized"   # 401 (revoked / invalid / missing key)
    NONE = "none"                   # not a policy denial — provider/model/other error


def classify_gateway_error(exc: Exception) -> PolicyDenial:
    """Map an exception raised by app.gateway.chat()/embed() to a policy reason."""
    if isinstance(exc, openai.AuthenticationError):           # 401
        return PolicyDenial.UNAUTHORIZED
    if isinstance(exc, openai.RateLimitError):                # 429
        return PolicyDenial.RATE_LIMITED
    if isinstance(exc, openai.BadRequestError):               # 400
        # Budget exhaustion is a 400; disambiguate from other 400s by the body.
        # VERIFY the exact substring/code LiteLLM returns for budget on the pin
        # (see README open question: "confirm the exact status/body").
        text = (str(exc) or "").lower()
        if "budget" in text or "exceeded" in text:
            return PolicyDenial.OVER_BUDGET
        return PolicyDenial.NONE
    return PolicyDenial.NONE
