"""ILLUSTRATIVE — spec for gateway/guardrails/injection.py.

Prompt-injection check, registered as a `pre_call` guardrail for chat only.
Scans the untrusted-data channel (text inside <untrusted_context>…</…>) AND the
user's question, independently. Channel separation is the primary defense
(agent.py wraps + the system prompt forbids obeying data); this scanner is
defense-in-depth.

Version-sensitive lines are marked `# VERIFY` against the pinned litellm image.
"""
import os
import re

from fastapi import HTTPException
from litellm.integrations.custom_guardrail import CustomGuardrail  # VERIFY import path

from .patterns import scan
from .policy import HDR_INJECTION_FLAGGED, run_with_policy, set_response_header


class PromptInjectionGuardrail(CustomGuardrail):
    def __init__(self, **kwargs):
        self.enabled = str(kwargs.pop("enabled", "true")).lower() == "true"
        self.fail_open = str(kwargs.pop("fail_open", os.environ.get("GUARDRAIL_FAIL_OPEN", "false"))).lower() == "true"
        self.timeout_ms = int(kwargs.pop("timeout_ms", 300))
        self.delimiter = kwargs.pop("data_delimiter", "untrusted_context")
        super().__init__(**kwargs)  # VERIFY: pop our custom keys so super() gets only what it expects

    def _split_channels(self, text: str) -> tuple[str, str]:
        """Return (data_channel, instruction_channel) from one user turn."""
        m = re.search(rf"<{self.delimiter}>(.*?)</{self.delimiter}>", text, re.S | re.I)
        if not m:
            return "", text
        data = m.group(1)
        instruction = (text[: m.start()] + text[m.end():])
        return data, instruction

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not self.enabled or call_type != "completion":
            return data

        async def _check():
            flagged: list[str] = []
            for msg in data.get("messages", []):
                content = msg.get("content")
                if msg.get("role") != "user" or not isinstance(content, str):
                    continue
                data_ch, instr_ch = self._split_channels(content)
                flagged += [f"data:{n}" for n in scan(data_ch)]
                flagged += [f"question:{n}" for n in scan(instr_ch)]
            if flagged:
                reason = "prompt_injection:" + ",".join(sorted(set(flagged)))
                raise HTTPException(
                    status_code=400,
                    detail={"guardrail": "prompt-injection", "action": "block", "reason": reason},
                )
            # Injection only owns the injection-flagged signal; it never writes
            # x-guardrail-action (README precedence note) so it can't downgrade a
            # PII redact->allow on the shared request. On the allow path it records
            # "false"; a real hit raises the 400 block above.
            set_response_header(data, HDR_INJECTION_FLAGGED, "false")
            return data

        # HTTPException must propagate (it IS the block); only infra errors hit fail-mode.
        return await run_with_policy(
            _check(), budget_ms=self.timeout_ms, fail_open=self.fail_open,
            guardrail="prompt-injection", passthrough=data, reraise=(HTTPException,),
        )
