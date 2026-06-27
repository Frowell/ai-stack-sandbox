"""ILLUSTRATIVE — spec for a NEW app/guardrails.py.

App-side mirror of the gateway's decision contract: the block sentinel, the
parsed decision the agent records on its span, and the header names (kept in
sync with gateway/guardrails/policy.py — design.md §5).
"""
from dataclasses import dataclass

# Must match gateway/guardrails/policy.py.
HDR_ACTION = "x-guardrail-action"
HDR_PII_COUNT = "x-guardrail-pii-redacted-count"
HDR_INJECTION_FLAGGED = "x-guardrail-injection-flagged"


class GuardrailBlocked(Exception):
    """Raised by gateway.chat() when the gateway returns a guardrail block (HTTP 400).

    Carries the auditable reason so generate_node can record it and refuse safely
    instead of the agent crashing on hostile input.
    """

    def __init__(self, reason: str, guardrail: str = ""):
        self.reason = reason
        self.guardrail = guardrail
        super().__init__(f"guardrail blocked ({guardrail}): {reason}")


class GuardrailBlockedError(GuardrailBlocked):
    """Block on the EMBEDDINGS path.

    embed() returns list[list[float]] and cannot carry a sentinel value, so a
    block on that alias (only reachable via fail-closed error/timeout, since PII
    redacts rather than blocks) is surfaced as this exception. Subclasses
    GuardrailBlocked so callers can catch either with one `except GuardrailBlocked`.
    retrieval.retrieve() catches it and degrades to empty context (returns []);
    app/ingest.py lets it abort the ingest loudly. (README "Embeddings block path" / AC3.)
    """


@dataclass(frozen=True)
class GuardrailDecision:
    action: str = "allow"            # allow | redact (block travels as GuardrailBlocked)
    pii_redacted_count: int = 0
    injection_flagged: bool = False
    reason: str = ""


def decision_from_headers(headers) -> GuardrailDecision:
    """Build a GuardrailDecision from a response's headers (httpx.Headers-like)."""
    def _get(name, default=""):
        return headers.get(name, default)

    return GuardrailDecision(
        action=_get(HDR_ACTION, "allow"),
        pii_redacted_count=int(_get(HDR_PII_COUNT, "0") or 0),
        injection_flagged=_get(HDR_INJECTION_FLAGGED, "false").lower() == "true",
        reason=_get("x-guardrail-reason", ""),
    )


def block_from_error(err) -> "GuardrailBlocked | None":
    """If an openai.BadRequestError is a guardrail block, return the sentinel; else None.

    A guardrail block has body {guardrail, action:"block", reason}. Non-guardrail
    400s (bad params, etc.) return None so the caller re-raises them unchanged.
    """
    body = getattr(err, "body", None)
    # SDK may nest the detail under "error"/"detail" depending on version. # VERIFY
    detail = (body or {}).get("detail", body) if isinstance(body, dict) else None
    if isinstance(detail, dict) and detail.get("action") == "block":
        return GuardrailBlocked(detail.get("reason", "blocked"), detail.get("guardrail", ""))
    return None
