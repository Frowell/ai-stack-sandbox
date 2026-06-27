"""ILLUSTRATIVE — spec for gateway/guardrails/patterns.py.

The default injection detector is regex/heuristics only (README open question:
"leaning regex-only for determinism"). Kept in its own module with no LiteLLM
import so unit tests can exercise it offline without standing up the gateway.

NOT exhaustive — best-effort by design. Exhaustive adversarial coverage is #17
(Safety & red-teaming), which builds its suite on top of this seam.
"""
import re

# (name, compiled pattern). Names land in the `reason` so a block is auditable.
INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ignore_previous", re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions\b", re.I)),
    ("disregard", re.compile(r"\bdisregard\s+(the\s+)?(system|previous|above)\b", re.I)),
    ("role_switch", re.compile(r"\byou\s+are\s+now\b|\bact\s+as\b|\bpretend\s+to\s+be\b", re.I)),
    ("system_override", re.compile(r"<\s*/?\s*system\s*>|^\s*system\s*:", re.I | re.M)),
    ("reveal_prompt", re.compile(r"\b(reveal|print|repeat|show)\s+(your\s+)?(system\s+)?prompt\b", re.I)),
    ("exfiltrate", re.compile(r"\b(send|post|exfiltrate|email)\b.*\b(api[_\s-]?key|secret|password)\b", re.I)),
    ("delimiter_breakout", re.compile(r"</\s*untrusted_context\s*>", re.I)),  # stray close tag in data
]

# Secrets the OUTPUT guardrail blocks on (not redact — block).
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b")),
]


def scan(text: str, patterns=INJECTION_PATTERNS) -> list[str]:
    """Return the names of every pattern that matches `text` (empty == clean)."""
    return [name for name, pat in patterns if pat.search(text or "")]
