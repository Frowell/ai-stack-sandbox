"""The beside-path seam.

Spans follow OpenTelemetry GenAI semantic conventions and export over OTLP to
*any* backend -- Langfuse, Phoenix, Honeycomb, Grafana Tempo. Set
OTEL_EXPORTER_OTLP_ENDPOINT to turn it on; leave it unset and the app runs
exactly the same, just without traces. The backend is a config choice, not a
code dependency.
"""
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
if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

trace.set_tracer_provider(_provider)
_tracer = trace.get_tracer("ai-stack-sandbox")


@contextmanager
def span(name: str, **attributes):
    with _tracer.start_as_current_span(name) as s:
        for key, value in attributes.items():
            if value is not None:
                s.set_attribute(key, value)
        yield s
