"""ILLUSTRATIVE — spec for spec 14, not wired-in code.

New module `app/score_sink.py`: the ONLY place that may import a vendor SDK.

  - ScoreSink protocol            : record(trace_id, name, value, comment=None)
  - NoopSink                      : default; does nothing
  - LangfuseSink                  : posts the score onto the trace via Langfuse's
                                    scores API (trace -> result). Imports the
                                    Langfuse SDK LAZILY, inside the factory, so
                                    the dependency is paid ONLY when selected.
  - get_score_sink()              : factory keyed on EVAL_SCORE_SINK (default noop)

Invariant this module exists to protect: with EVAL_SCORE_SINK unset, importing
app.evals / app.observability must import NO vendor SDK. The lazy import below is
what makes that true. (Phoenix has no scores API; with Phoenix selected as the
backend you simply leave EVAL_SCORE_SINK at its noop default.)

Do NOT import from this file. Port the pieces into app/score_sink.py.
"""
from __future__ import annotations

import logging
import os
from typing import Protocol

log = logging.getLogger(__name__)


class ScoreSink(Protocol):
    def record(
        self, trace_id: str, name: str, value: float, *, comment: str | None = None
    ) -> None: ...


class NoopSink:
    """Default. The OTLP (result->trace) path works without any sink; this just
    skips the optional trace->result push."""

    def record(self, trace_id: str, name: str, value: float, *, comment: str | None = None) -> None:
        return None


class LangfuseSink:
    """Posts a numeric score keyed by traceId. Langfuse stores scores keyed by
    traceId independent of ingest order, so a late-arriving trace still binds."""

    def __init__(self, client) -> None:  # client: langfuse.Langfuse
        self._client = client

    def record(self, trace_id: str, name: str, value: float, *, comment: str | None = None) -> None:
        # Failures are NON-FATAL: a flaky backend must never fail the eval gate.
        try:
            self._client.create_score(trace_id=trace_id, name=name, value=value, comment=comment)
        except Exception as e:  # noqa: BLE001 - deliberately broad; log + carry on
            log.warning("score_sink: failed to record score for trace %s: %s", trace_id, e)


def get_score_sink() -> ScoreSink:
    """Select the sink from EVAL_SCORE_SINK. Default 'noop'. The Langfuse import
    happens HERE (lazily) so the OTLP path never imports a vendor SDK."""
    choice = os.environ.get("EVAL_SCORE_SINK", "noop").lower()
    if choice in ("", "noop", "none", "off"):
        return NoopSink()
    if choice == "langfuse":
        from langfuse import Langfuse  # lazy: only imported when selected

        # Langfuse SDK reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
        # from env (same fixed dev keys as the OTLP Basic header).
        return LangfuseSink(Langfuse())
    log.warning("score_sink: unknown EVAL_SCORE_SINK=%r, falling back to noop", choice)
    return NoopSink()
