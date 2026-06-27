"""ILLUSTRATIVE — spec for gateway/guardrails/pii.py.

One class, two registrations (design.md §2):
  - mode: pre_call   -> redact PII on the outbound request (chat AND embeddings)
  - mode: post_call  -> redact PII on the response (chat); BLOCK on secrets

Uses Presidio's Python API directly (analyzer + anonymizer) so we control the
redaction COUNT surfaced on the response (the built-in `presidio` guardrail does
not — design.md §1). Typed placeholders preserve shape (<EMAIL_ADDRESS>) to limit
quality degradation, per README risk mitigation.

Version-sensitive lines marked `# VERIFY`.
"""
import os

from fastapi import HTTPException
from litellm.integrations.custom_guardrail import CustomGuardrail  # VERIFY import path
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

from .patterns import scan, SECRET_PATTERNS
from .policy import (
    HDR_ACTION, HDR_PII_COUNT, HDR_REASON, run_with_policy, set_response_header,
)

_analyzer = AnalyzerEngine()
_anonymizer = AnonymizerEngine()


class PIIGuardrail(CustomGuardrail):
    def __init__(self, **kwargs):
        self.enabled = str(kwargs.pop("enabled", "true")).lower() == "true"
        self.fail_open = str(kwargs.pop("fail_open", os.environ.get("GUARDRAIL_FAIL_OPEN", "false"))).lower() == "true"
        self.timeout_ms = int(kwargs.pop("timeout_ms", 300))
        self.entities = kwargs.pop("entities", ["EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "PERSON"])
        self.score_threshold = float(kwargs.pop("score_threshold", 0.5))
        self.block_on_secret = bool(kwargs.pop("block_on_secret", False))
        super().__init__(**kwargs)

    def _redact(self, text: str) -> tuple[str, int]:
        """Return (redacted_text, n_entities). Typed placeholders preserve shape."""
        if not isinstance(text, str) or not text:
            return text, 0
        found = [r for r in _analyzer.analyze(text=text, entities=self.entities, language="en")
                 if r.score >= self.score_threshold]
        if not found:
            return text, 0
        out = _anonymizer.anonymize(
            text=text, analyzer_results=found,
            operators={"DEFAULT": OperatorConfig("replace", {"new_value": ""}),  # placeholder filled below
                       **{e: OperatorConfig("replace", {"new_value": f"<{e}>"}) for e in self.entities}},
        )
        return out.text, len(found)

    # ---- INPUT (pre_call): chat messages AND embedding inputs ---------------
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not self.enabled:
            return data

        async def _do():
            total = 0
            if call_type == "completion":
                for msg in data.get("messages", []):
                    if isinstance(msg.get("content"), str):
                        msg["content"], n = self._redact(msg["content"])
                        total += n
            elif call_type == "embeddings":
                inp = data.get("input")
                items = [inp] if isinstance(inp, str) else (inp or [])
                redacted = []
                for s in items:
                    r, n = self._redact(s)
                    redacted.append(r)
                    total += n
                data["input"] = redacted[0] if isinstance(inp, str) else redacted
            set_response_header(data, HDR_PII_COUNT, str(total))
            set_response_header(data, HDR_ACTION, "redact" if total else "allow")
            return data

        return await run_with_policy(
            _do(), budget_ms=self.timeout_ms, fail_open=self.fail_open,
            guardrail="pii-input", passthrough=data,
        )

    # ---- OUTPUT (post_call): redact PII in the response; block on secrets ----
    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        if not self.enabled:
            return response

        async def _do():
            try:
                msg = response.choices[0].message
                text = msg.content or ""
            except (AttributeError, IndexError):
                return response  # nothing to scan (e.g. tool-only response)

            if self.block_on_secret and scan(text, SECRET_PATTERNS):
                raise HTTPException(
                    status_code=400,
                    detail={"guardrail": "pii-output", "action": "block", "reason": "secret_in_output"},
                )
            redacted, n = self._redact(text)
            if n:
                msg.content = redacted  # VERIFY response mutation shape vs pinned litellm
                # Output hook may ESCALATE the action (precedence-merged in
                # set_response_header); it can't downgrade an input-side redact.
                set_response_header(data, HDR_ACTION, "redact")
                set_response_header(data, HDR_REASON, "pii_redacted_in_output")
            return response

        return await run_with_policy(
            _do(), budget_ms=self.timeout_ms, fail_open=self.fail_open,
            guardrail="pii-output", passthrough=response, reraise=(HTTPException,),
        )
