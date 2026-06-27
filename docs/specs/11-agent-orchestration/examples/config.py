"""ILLUSTRATIVE — proposed additions to app/config.py. Not wired in.

Mirrors the existing frozen-dataclass / env-driven pattern. With
ORCHESTRATION_MODE unset (the default 'single'), none of the other fields change
any behaviour, so this addition is a no-op for the current app.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # ... all existing fields unchanged (database_url, gateway_*, chat_model, ...)

    # --- new: agent orchestration ---
    orchestration_mode: str = os.environ.get("ORCHESTRATION_MODE", "single")  # single | multi
    max_depth: int = int(os.environ.get("MAX_DEPTH", "2"))
    max_iterations: int = int(os.environ.get("MAX_ITERATIONS", "6"))
    # per-run total-token ceiling; 0 == unlimited (depth/iteration caps still apply)
    token_budget: int = int(os.environ.get("TOKEN_BUDGET", "20000"))

    @property
    def recursion_limit(self) -> int:
        """LangGraph backstop, deliberately ABOVE the configured cap ceiling so the
        run ends via the `truncate` path before GraphRecursionError can fire.
        ~2 graph steps per supervisor turn (supervisor + specialist) + slack."""
        env = os.environ.get("RECURSION_LIMIT")
        return int(env) if env else 2 * self.max_iterations + 5
