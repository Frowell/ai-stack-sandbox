"""ILLUSTRATIVE — proposed shape of the `multi` topology in app/agent.py. Not wired in.

Shows: the flag-branched build_graph(), the supervisor + specialist + truncate
nodes, the `_route()` conditional edge with caps + invalid-route fallback, the
lazy PostgresSaver-backed graph (no DB connection at import), and the additive
resumable API. `single` mode is byte-for-byte today's retrieve->generate compile.

Real signatures referenced:
    app.gateway.chat(messages, **kwargs) -> str
    app.gateway.chat_with_usage(messages, **kwargs) -> ChatResult   # see gateway_usage.py
    app.observability.span(name, **attributes)  (contextmanager)
    app.retrieval.retrieve(query, top_n=4, pool=10) -> list[tuple[int, str]]
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .config import settings
from .gateway import chat, chat_with_usage
from .observability import span
from .retrieval import retrieve

VALID_ROUTES = {"retrieve", "research", "synthesize", "done"}


class State(TypedDict, total=False):
    question: str
    context: list[tuple[int, str]]
    answer: str
    next: str
    depth: int
    iterations: int
    token_budget: int
    tokens_used: int
    truncated: bool
    notes: list[str]


# --------------------------------------------------------------------------- #
# single mode: unchanged from today (subset of State; no orchestration fields)
# --------------------------------------------------------------------------- #
def retrieve_node(state: State) -> dict:
    return {"context": retrieve(state["question"])}


def generate_node(state: State) -> dict:
    ctx = "\n\n".join(f"[{i}] {c}" for i, c in state["context"])
    with span("generate", **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "chat"}):
        answer = chat(
            [
                {"role": "system", "content": "Answer using only the provided context. Cite [id]s. If insufficient, say so."},
                {"role": "user", "content": f"Context:\n{ctx}\n\nQuestion: {state['question']}"},
            ]
        )
    return {"answer": answer}


def _build_single():
    g = StateGraph(State)
    g.add_node("retrieve", retrieve_node)
    g.add_node("generate", generate_node)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", END)
    return g.compile()  # no checkpointer; no DB connection


# --------------------------------------------------------------------------- #
# multi mode: supervisor + specialists + truncate
# --------------------------------------------------------------------------- #
def supervisor_node(state: State) -> dict:
    """Decide the next route and advance the iteration counter. Deterministic rule
    for the proof slice (cheap + hermetic to test); an LLM router can layer on
    later. Caps are enforced in _route(), not here."""
    it = state.get("iterations", 0) + 1
    if not state.get("context"):
        nxt = "retrieve"
    elif not state.get("notes"):
        nxt = "research"
    elif not state.get("answer"):
        nxt = "synthesize"
    else:
        nxt = "done"
    return {"iterations": it, "next": nxt}


def research_node(state: State) -> dict:
    ctx = "\n\n".join(f"[{i}] {c}" for i, c in state.get("context", []))
    with span("research", **{"gen_ai.operation.name": "chat"}):
        res = chat_with_usage(
            [
                {"role": "system", "content": "Extract the key facts from the context relevant to the question."},
                {"role": "user", "content": f"Context:\n{ctx}\n\nQuestion: {state['question']}"},
            ]
        )
    return {
        "notes": [*state.get("notes", []), res.text],
        "tokens_used": state.get("tokens_used", 0) + res.total_tokens,
    }


def synthesize_node(state: State) -> dict:
    notes = "\n".join(state.get("notes", []))
    with span("synthesize", **{"gen_ai.operation.name": "chat"}):
        res = chat_with_usage(
            [
                {"role": "system", "content": "Write the final answer from these notes. Cite [id]s. If insufficient, say so."},
                {"role": "user", "content": f"Notes:\n{notes}\n\nQuestion: {state['question']}"},
            ]
        )
    return {"answer": res.text, "tokens_used": state.get("tokens_used", 0) + res.total_tokens}


def truncate_node(state: State) -> dict:
    """Terminal: clean partial result instead of an exception/recursion crash."""
    partial = state.get("answer") or (
        "Stopped early (budget/iteration cap reached) with partial findings: "
        + " ".join(state.get("notes", []))
    ).strip()
    return {"truncated": True, "answer": partial or "Stopped early before producing an answer."}


def _route(state: State) -> str:
    """Conditional edge. Guards win over the supervisor's intent; an unrecognised
    `next` falls back to truncate rather than crashing the graph."""
    budget = state.get("token_budget", 0)
    if (
        state.get("iterations", 0) >= settings.max_iterations
        or state.get("depth", 0) > settings.max_depth
        or (budget and state.get("tokens_used", 0) >= budget)
    ):
        return "truncate"
    nxt = state.get("next", "")
    if nxt == "done":
        return END
    if nxt not in VALID_ROUTES:        # invalid / out-of-range route decision
        return "truncate"
    return nxt


def _build_multi(checkpointer=None):
    g = StateGraph(State)
    g.add_node("supervisor", supervisor_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("research", research_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("truncate", truncate_node)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges(
        "supervisor",
        _route,
        {"retrieve": "retrieve", "research": "research", "synthesize": "synthesize",
         "truncate": "truncate", END: END},
    )
    for worker in ("retrieve", "research", "synthesize"):
        g.add_edge(worker, "supervisor")   # always return to the supervisor
    g.add_edge("truncate", END)
    return g.compile(checkpointer=checkpointer)


def build_graph():
    """Flag-branched factory. `single` is the import-time, connection-free path."""
    if settings.orchestration_mode == "multi":
        return _build_multi()  # in-memory checkpointer; use get_graph() for durable
    return _build_single()


# import-time graph: only safe because single mode opens no DB connection.
GRAPH = build_graph()

# --------------------------------------------------------------------------- #
# lazy, durable graph for multi mode (no DB connection at import time)
# --------------------------------------------------------------------------- #
_DURABLE_GRAPH = None


def get_graph():
    """Build + cache the multi graph with a PostgresSaver on first use. Calling
    setup() here is idempotent (safe to repeat); see checkpointer_setup.py."""
    global _DURABLE_GRAPH
    if settings.orchestration_mode != "multi":
        return GRAPH
    if _DURABLE_GRAPH is None:
        from langgraph.checkpoint.postgres import PostgresSaver
        saver = PostgresSaver.from_conn_string(settings.database_url).__enter__()
        saver.setup()  # idempotent; creates checkpoints/checkpoint_writes/checkpoint_blobs
        _DURABLE_GRAPH = _build_multi(checkpointer=saver)
    return _DURABLE_GRAPH


# --------------------------------------------------------------------------- #
# resumable API (additive) — ask() keeps (question: str) -> str
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AskResult:
    answer: str
    thread_id: str
    truncated: bool


def ask_resumable(question: str | None, thread_id: str | None = None) -> AskResult:
    """Run (or resume) a thread. Pass the same thread_id to resume after a crash;
    pass question=None to resume an in-flight thread from its last checkpoint."""
    tid = thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": tid}, "recursion_limit": settings.recursion_limit}
    graph = get_graph()
    payload = None if question is None else {
        "question": question, "token_budget": settings.token_budget,
        "depth": 0, "iterations": 0, "tokens_used": 0, "notes": [], "context": [],
    }
    with span("agent.run", **{"gen_ai.operation.name": "agent", "input.question": question, "thread_id": tid}):
        final = graph.invoke(payload, config)
    return AskResult(answer=final.get("answer", ""), thread_id=tid, truncated=bool(final.get("truncated")))


def ask(question: str) -> str:
    """UNCHANGED public contract: (question: str) -> str. Delegates in multi mode."""
    if settings.orchestration_mode == "multi":
        return ask_resumable(question).answer
    with span("agent.run", **{"gen_ai.operation.name": "agent", "input.question": question}):
        return GRAPH.invoke({"question": question})["answer"]
