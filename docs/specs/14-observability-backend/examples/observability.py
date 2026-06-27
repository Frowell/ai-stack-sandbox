"""ILLUSTRATIVE — spec for spec 14, not wired-in code.

Shows the additions to `app/observability.py`:
  - flush()        : force_flush the BatchSpanProcessor (no-op when no exporter)
  - atexit shutdown: belt-and-braces backstop for any exit path
  - OTEL_CAPTURE_CONTENT gating inside span() so a single switch suppresses ALL
    content-bearing attributes (input.question, retrieval.query, and any added
    later) before they touch a span.

Unchanged from today: the provider is ALWAYS installed (ALWAYS_ON sampler); only
the *exporter* is gated on OTEL_EXPORTER_OTLP_ENDPOINT — so get_current_span()
yields a valid non-zero trace_id whether or not a backend is configured.

Do NOT import from this file. Port the pieces into app/observability.py.
"""
import atexit
import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_provider = TracerProvider(
    resource=Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME", "ai-stack-sandbox")}
    )
)

# Only attach an exporter when an endpoint is configured. No endpoint => spans
# are created but go nowhere (cheap), so tracing is genuinely optional.
# Bound export_timeout_millis so a slow/unreachable backend cannot make the
# atexit shutdown (which force-flushes) hang ~30s on every `make eval`/`make ask`.
if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
    _provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(), export_timeout_millis=5000)
    )

trace.set_tracer_provider(_provider)
_tracer = trace.get_tracer("ai-stack-sandbox")

# --- NEW: content-capture toggle (PII posture) -----------------------------
# Default ON for the sandbox; document "turn off in any environment with real
# data". Implemented once here so the single switch covers every content attr.
_CAPTURE_CONTENT = os.environ.get("OTEL_CAPTURE_CONTENT", "1").lower() not in ("0", "false", "no")
_CONTENT_KEYS = {"input.question", "retrieval.query"}  # extend as content attrs are added


def capture_content() -> bool:
    """Whether content-bearing data may leave the process. Gates span attributes
    here, and is reused by evals.run() to gate the score_sink comment (the other
    channel that can carry content to the backend)."""
    return _CAPTURE_CONTENT


# --- NEW: reliable delivery from short-lived processes ---------------------
def flush(timeout_millis: int = 5000) -> None:
    """Force-export any buffered spans. Call this in a finally inside evals.run()
    (covers pytest, which never reaches __main__) and at the end of CLI mains.
    force_flush() is a no-op when no exporter is attached, so it is safe to call
    with OTEL_EXPORTER_OTLP_ENDPOINT unset.

    A short, explicit timeout is passed (force_flush's own default is 30_000ms) so
    a reachable-but-slow or unreachable backend cannot block the exit path of every
    `make eval` / `make ask`. Export is best-effort: the run already produced its
    result, so a flush that times out is non-fatal (force_flush returns False).
    """
    _provider.force_flush(timeout_millis)


# Backstop: flush + clean shutdown on any normal interpreter exit.
atexit.register(_provider.shutdown)


@contextmanager
def span(name: str, **attributes):
    with _tracer.start_as_current_span(name) as s:
        for key, value in attributes.items():
            if value is None:
                continue
            # NEW: drop content-bearing attributes when capture is disabled, so
            # sensitive text never reaches the span (and thus never the backend).
            if key in _CONTENT_KEYS and not _CAPTURE_CONTENT:
                continue
            s.set_attribute(key, value)
        yield s
