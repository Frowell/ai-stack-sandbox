"""ILLUSTRATIVE — spec for gateway/guardrails/policy.py.

Shared fail-mode wrapper + the response-header names the app reads back
(app/guardrails.py mirrors these constants). Centralizing the policy means
"fail-closed by default, dev-only fail-open" is one code path, tested once.
"""
import asyncio
import logging

from fastapi import HTTPException

log = logging.getLogger("guardrails")

# Header contract shared with app/guardrails.py (design.md §5).
HDR_ACTION = "x-guardrail-action"                    # allow | redact | block
HDR_PII_COUNT = "x-guardrail-pii-redacted-count"
HDR_INJECTION_FLAGGED = "x-guardrail-injection-flagged"
HDR_REASON = "x-guardrail-reason"                    # set on redact/allow; block reason travels in the 400 body

# x-guardrail-action precedence (README "Decision propagation"): multiple
# guardrails write decision metadata on the SAME request, so a later hook may only
# *escalate* the action, never downgrade it. Otherwise an allow-path hook running
# after a real redaction would silently overwrite redact->allow.
_ACTION_RANK = {"allow": 0, "redact": 1, "block": 2}


def set_response_header(data: dict, name: str, value: str) -> None:
    """Stash a header LiteLLM will emit on the 200 response.

    For HDR_ACTION the write is merged by precedence (block > redact > allow): an
    existing higher-ranked action is never clobbered by a lower one. All other
    headers are last-writer-wins.

    # VERIFY: the exact mechanism for 'guardrail sets a response header' has
    # churned across litellm versions. Confirm ONE of these against the pinned
    # image and use it consistently:
    #   data.setdefault("litellm_metadata", {}).setdefault("response_headers", {})[name] = value
    #   -- or a logging callback that copies response._hidden_params -> headers.
    """
    headers = data.setdefault("litellm_metadata", {}).setdefault("response_headers", {})
    if name == HDR_ACTION:
        current = headers.get(HDR_ACTION, "allow")
        if _ACTION_RANK.get(value, 0) <= _ACTION_RANK.get(current, 0):
            return  # don't downgrade / don't redundantly rewrite an equal-or-higher action
    headers[name] = value


async def run_with_policy(coro, *, budget_ms: int, fail_open: bool, guardrail: str,
                          passthrough, reraise: tuple = ()):
    """Run a guardrail body under a timeout with explicit fail-mode.

    - Block decisions (HTTPException) are re-raised (they ARE the answer).
    - Any other error/timeout: fail CLOSED (raise 400 block) unless fail_open.
    """
    try:
        return await asyncio.wait_for(coro, budget_ms / 1000)
    except reraise:
        raise
    except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 - guardrails must never leak through
        if fail_open:
            # Logged LOUDLY on every invocation so a misconfigured prod can't hide it.
            log.warning("GUARDRAIL FAIL-OPEN: %s passed through after error: %r", guardrail, e)
            return passthrough
        raise HTTPException(
            status_code=400,
            detail={"guardrail": guardrail, "action": "block", "reason": f"guardrail_error:{type(e).__name__}"},
        )
