"""ILLUSTRATIVE — proposed changes to `app/agent.py` for spec 17. Not wired in.

Only the deltas vs the current file are shown in context:
  1. State + ask() gain an optional `inject_context` (post-retrieval splice, §4a).
  2. retrieve_node splices the synthetic injected doc onto the REAL retrieved
     context (so the real generate_node + real model call still run).
  3. generate_node routes the model output through app.safety.handle() so a refusal
     / empty-success becomes a deterministic fallback + a `safety.refusal` span --
     never silently returned as an answer.

Everything else (build_graph, edges) is unchanged from today's app/agent.py.
"""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .gateway import chat
from .observability import span
from .retrieval import retrieve
from .safety import handle  # NEW (see app_safety.py)

# Sentinel id for a spliced injected doc. Real documents.id is a serial >= 1, so -1
# can never collide and is visually obvious if the model cites [-1] (design.md §4a).
_INJECT_DOC_ID = -1


class State(TypedDict):
    question: str
    context: list[tuple[int, str]]
    answer: str
    inject_context: str | None  # NEW: post-retrieval splice payload (None in prod)


def retrieve_node(state: State) -> dict:
    ctx = retrieve(state["question"])  # the REAL ranked corpus, unchanged
    inj = state.get("inject_context")
    if inj:
        # Append the synthetic untrusted doc AFTER real retrieval. This exercises the
        # real generate_node prompt assembly + real model call without mocking the
        # model; it does NOT prove the retriever surfaces a planted doc (that is the
        # end-to-end ingest case, design.md §4b).
        ctx = ctx + [(_INJECT_DOC_ID, inj)]
    return {"context": ctx}


def generate_node(state: State) -> dict:
    ctx = "\n\n".join(f"[{doc_id}] {content}" for doc_id, content in state["context"])
    with span("generate", **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "chat"}):
        # chat() is a thin transport: raises on transport failure (spec 01), returns
        # raw model content otherwise. We do NOT refusal-handle inside chat() (that
        # would corrupt the judge calls -- design.md §3).
        content = chat(
            [
                {
                    "role": "system",
                    "content": "Answer using only the provided context. Cite [id]s. If the context is insufficient, say so.",
                },
                {"role": "user", "content": f"Context:\n{ctx}\n\nQuestion: {state['question']}"},
            ]
        )
        # Refusal / empty-success handling happens HERE, once, in the agent.
        # `slice` is an eval-only attribute and is absent (None) on the prod path.
        answer = handle(content)
    return {"answer": answer}


def build_graph():
    g = StateGraph(State)
    g.add_node("retrieve", retrieve_node)
    g.add_node("generate", generate_node)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", END)
    return g.compile()


GRAPH = build_graph()


def ask(question: str, inject_context: str | None = None) -> str:
    """Prod call sites pass only `question` (inject_context defaults to None, so the
    splice is inert). The safety harness passes inject_context for the §4a path."""
    with span("agent.run", **{"gen_ai.operation.name": "agent", "input.question": question}):
        return GRAPH.invoke({"question": question, "inject_context": inject_context})["answer"]
