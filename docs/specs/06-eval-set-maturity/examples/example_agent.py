"""ILLUSTRATIVE — spec for spec 06, would change app/agent.py. Not wired in.

The sampling + usage seam (README §3, design.md §1a). Today:
  - ask(question) -> str         takes no sampling args
  - generate_node                hard-codes chat([...]) with no kwargs
so the eval/baseline path can neither request temperature=0 / N samples nor read
back token usage. The graph topology and the default behaviour stay identical.

Change shape:
  - ask(question, *, gen_kwargs=None, return_meta=False)
      gen_kwargs   -> sampling params placed in graph state, forwarded to chat()
      return_meta  -> also return per-call usage for cost capture
  - generate_node reads state["gen_kwargs"] and (when capturing) opts into the
    gateway usage seam.

With gen_kwargs=None and return_meta=False (the defaults), ask() behaves exactly
as today and the hot path is byte-for-byte unchanged.

The exact signature is co-owned with PR #1 (N-samples + pins live there).  # PR#1

Do NOT import from this file. Port the change into app/agent.py.
"""
from __future__ import annotations

from typing import TypedDict

from .gateway import chat
from .observability import span
# build_graph()/GRAPH/State exist already; State gains two optional keys.


class State(TypedDict, total=False):
    question: str
    context: list[tuple[int, str]]
    answer: str
    # --- new, optional (total=False) ---
    gen_kwargs: dict          # sampling params, e.g. {"temperature": 0}
    capture_usage: bool       # when True, generate_node records usage into state
    usage: object             # CompletionUsage, populated only when capture_usage


def generate_node(state: State) -> dict:
    ctx = "\n\n".join(f"[{doc_id}] {content}" for doc_id, content in state["context"])
    gen_kwargs = state.get("gen_kwargs") or {}
    messages = [
        {"role": "system",
         "content": "Answer using only the provided context. Cite [id]s. "
                    "If the context is insufficient, say so."},
        {"role": "user", "content": f"Context:\n{ctx}\n\nQuestion: {state['question']}"},
    ]
    with span("generate", **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "chat"}):
        if state.get("capture_usage"):
            answer, usage = chat(messages, return_usage=True, **gen_kwargs)
            return {"answer": answer, "usage": usage}
        answer = chat(messages, **gen_kwargs)   # default path, unchanged
    return {"answer": answer}


def ask(question: str, *, gen_kwargs: dict | None = None, return_meta: bool = False):
    """Default: ask(question) -> str  (UNCHANGED).

    return_meta=True -> returns (answer, {"usage": CompletionUsage|None}) so the
    eval harness can compute per-case cost. gen_kwargs threads sampling params
    (e.g. temperature=0) to the served model for a reproducible baseline.
    """
    with span("agent.run", **{"gen_ai.operation.name": "agent", "input.question": question}):
        initial: State = {"question": question}
        if gen_kwargs:
            initial["gen_kwargs"] = gen_kwargs
        if return_meta:
            initial["capture_usage"] = True
        final = GRAPH.invoke(initial)  # noqa: F821 (GRAPH defined in real module)
    if return_meta:
        return final["answer"], {"usage": final.get("usage")}
    return final["answer"]
