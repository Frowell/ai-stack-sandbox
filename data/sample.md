# Mature AI Stacks

## Observability

Observability sits beside the hot path and ingests spans asynchronously[^otel].
It captures the full execution graph[^graph] that a gateway's request log cannot.

| Layer    | Standard           |
| -------- | ------------------ |
| Tracing  | OpenTelemetry      |
| Gateway  | OpenAI-compatible  |

## Evaluation

Evaluation runs as CI gates rather than vibes[^eval]: a failing eval blocks the merge.

[^otel]: OpenTelemetry GenAI semantic conventions are the tracing standard, exported over OTLP.
[^graph]: The execution graph records retrieval, tool calls, and reasoning steps as nested spans.
[^eval]: Golden datasets curated from production traces act as regression suites with LLM-as-judge scoring.
