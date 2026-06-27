"""ILLUSTRATIVE — span continuity across a resume. Not wired in.

Within a single process run, per-node spans already nest under the run span
because app.observability.span() uses start_as_current_span and node bodies run
inside the `agent.run` context. The only gap is RESUME in a fresh process, where
the original run span no longer exists in memory.

This sketch shows option (2) from design.md §6: a linked-but-new trace. On first
run we stash the run's trace_id in checkpoint state; on resume we start a new run
span, attach a Link to the original trace, and set a `resumed_from` attribute.
Option (1) (restore parent SpanContext) is noted there as a follow-up.
"""
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Link, NonRecordingSpan, SpanContext, TraceFlags

_tracer = trace.get_tracer("ai-stack-sandbox")


def current_trace_id_hex() -> str:
    """Stash this into State on the first run so a resume can link back to it."""
    return format(trace.get_current_span().get_span_context().trace_id, "032x")


@contextmanager
def resumed_run_span(name: str, resumed_from_trace_id_hex: str | None, **attributes):
    links = []
    attrs = dict(attributes)
    if resumed_from_trace_id_hex:
        ctx = SpanContext(
            trace_id=int(resumed_from_trace_id_hex, 16),
            span_id=0x1,  # placeholder root; real impl stores the run span_id too
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        links.append(Link(ctx))
        attrs["resumed_from"] = resumed_from_trace_id_hex
    with _tracer.start_as_current_span(name, links=links) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, v)
        yield s


# Usage sketch inside ask_resumable():
#   first run:  state["run_trace_id"] = current_trace_id_hex()  (persisted by the saver)
#   resume:     with resumed_run_span("agent.run", state.get("run_trace_id"), thread_id=tid): ...
