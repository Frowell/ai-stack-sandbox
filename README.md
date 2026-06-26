# ai-stack-sandbox

A runnable, deliberately small slice of the "mature AI stack" — enough to poke at
every seam without standing up a production platform. The point isn't the tools;
it's the *seams between them*, which is what actually separates a mature stack
from a pile of services.

It boots four containers and gives you a question-answering agent you can trace,
evaluate, and re-wire by editing config rather than code.

## What's in it, and how each piece maps to the architecture

| Layer | This sandbox | The seam it demonstrates |
|---|---|---|
| **Gateway (hot path)** | LiteLLM container | App calls aliases `chat`/`embeddings`; provider is a config choice. Even embeddings route through it. |
| **Orchestration** | LangGraph (`retrieve → generate`) | A real graph with typed state; the smallest honest version of a multi-node agent. |
| **Retrieval substrate** | Postgres + pgvector | Hybrid: dense (`<=>` cosine) + sparse (full-text), merged with Reciprocal Rank Fusion. |
| **Cache / working memory** | Redis | Query-embedding cache; degrades gracefully if absent. |
| **Observability (beside path)** | OpenTelemetry SDK → OTLP | GenAI-convention spans exported to *any* backend (Langfuse, Phoenix, Honeycomb). Off by default. |
| **Evaluation** | `app/evals.py` + pytest | Golden set scored by keyword overlap + LLM-as-judge; **non-zero exit fails CI**. |

## Quickstart

**Prereqs:** Docker, and either the VS Code Dev Containers extension *or* just
`docker compose`.

1. **Configure.** The scaffold already created `.env` for you; just add a key.
   (If you cloned via git instead, run `cp .env.example .env` first, since `.env`
   is gitignored.) Compose tolerates a missing `.env`, so the container will still
   build without it -- you just need the key set before any model call.
   ```bash
   # edit .env and set:
   #   OPENAI_API_KEY=sk-...   (covers both chat and embeddings)
   ```

2. **Start the stack.** Two paths:

   *Plain Docker (most robust):* from the project folder,
   ```bash
   docker compose up -d --build         # builds the app, pulls litellm/postgres/redis
   docker compose exec app uv sync      # create the in-container venv
   ```

   *VS Code Dev Containers:* "Reopen in Container". VS Code starts all four
   services and drops you into a terminal inside the `app` container with deps
   already synced. Manage the stack lifecycle (up/down/logs) from a separate host
   terminal — the container intentionally doesn't carry a Docker CLI.

3. **Explore the loop.** Inside the `app` container (VS Code terminal), or via
   `docker compose exec app ...` from the host:
   ```bash
   uv run python -m app.ingest                              # embed corpus → pgvector
   uv run python -m app.agent "why put a gateway in the hot path?"
   uv run python -m app.evals                               # run the quality gate
   ```
   (`make ingest` / `make ask Q="..."` / `make eval` are shorthand if you have
   `make` available.)

That's the whole exploration surface: ingest → ask → eval.

## The seams, concretely

**Swap the model provider without touching code.** Open
`gateway/litellm_config.yaml`, comment the OpenAI `chat` block, uncomment the
Anthropic one, set `ANTHROPIC_API_KEY` in `.env`, restart the gateway. The agent
is unchanged — that decoupling is the reason the gateway sits in the hot path.

**Send traces to your real observability backend.** Set two env vars (see
`.env.example`) to point OTLP at Langfuse, Phoenix, or anything OTel-native. The
app emits spans against the GenAI semantic conventions either way; the backend is
a config choice, not a code dependency. Leave them unset and everything still runs.

**Make the eval a merge gate.** `uv run pytest` (or `make test`) runs the golden
set and fails when the mean score drops below threshold. Drop this into CI and a
quality regression blocks the PR — the discipline, not just the dashboard.

## What's deliberately stubbed (the next layers to add)

This is a slice, not the whole stack. The obvious extensions, each a clean insertion point:

- **Reranker** — `rerank()` in `app/retrieval.py` is identity today. Drop in a
  cross-encoder or a Cohere/Voyage call.
- **Tool-call policy / audit** — there's no agent tool layer yet. A policy gate on
  tool calls (OCSF-shaped audit, allow/deny on parameters) is the natural next
  module, and arguably the highest-value one once the agent gains tools.
- **MCP gateway** — to govern tool access the way the LLM gateway governs model
  access, front tools with an MCP server and route through it.
- **Graph memory** — Redis + pgvector cover working and semantic memory; add Neo4j
  only if your domain is relationship-heavy.
- **Batteries-included evals** — swap the hand-rolled scorer for Ragas, DeepEval,
  or Promptfoo when you want faithfulness/context-precision metrics out of the box.

## Service reference

| Service | Port | Purpose |
|---|---|---|
| litellm | 4000 | Gateway (OpenAI-compatible) |
| postgres | 5432 | pgvector + full-text |
| redis | 6379 | Embedding cache |

`make logs` tails the gateway; `make psql` opens a shell on the vector store;
`make down` tears it all down.
