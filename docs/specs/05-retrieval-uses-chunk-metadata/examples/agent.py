"""ILLUSTRATIVE — spec for app/agent.py (NOT wired in).

Two changes from today:
  1. State.context is retyped to list[RetrievedChunk] (carrier from retrieval.py).
  2. The inline context join inside generate_node is extracted into a pure,
     importable `render_context()` helper so it can be unit-tested WITHOUT the LLM
     (this is the primary feature proof — see testing.md / criterion 2).

build_graph / ask / __main__ are unchanged and elided.
"""
from typing import TypedDict

from .gateway import chat
from .observability import span
from .retrieval import RetrievedChunk, retrieve


class State(TypedDict):
    question: str
    context: list[RetrievedChunk]   # was: list[tuple[int, str]]
    answer: str


def _provenance(meta: dict) -> str:
    """Build the parenthetical body from meta, or '' when nothing is displayable.

    Deterministic order: section first, then page (design.md §3). Both keys are
    read by TRUTHINESS via .get() (README contract: a missing key AND a falsy
    value are both skipped), and never indexed, so an absent/empty page emits
    nothing and never raises. `section` is str()-coerced defensively so a future
    list-valued breadcrumb renders (ugly, but no TypeError in the join) — this is
    the behavior the README's accepted-risk on `section`/`page` value types
    promises; the canonical model still guarantees a string today.
    """
    parts: list[str] = []
    section = meta.get("section")
    if section:
        parts.append(str(section))
    page = meta.get("page")
    if page:                                  # truthy: skips None AND 0/"" (README)
        parts.append(f"p.{page}")
    return ", ".join(parts)


def render_context(chunks: list[RetrievedChunk]) -> str:
    """Pure, importable, no-LLM. Renders the context block generate_node passes
    to chat(). For chunks with no displayable meta key this is BYTE-IDENTICAL to
    today's  "\\n\\n".join(f"[{doc_id}] {content}" ...)  — including the "\\n\\n"
    separator — so the all-unstructured prompt does not shift (design.md §4).
    """
    lines: list[str] = []
    for c in chunks:
        prov = _provenance(c.meta or {})   # belt-and-suspenders vs meta=None (design.md §2)
        if prov:
            lines.append(f"[{c.id}] ({prov}) {c.content}")
        else:
            lines.append(f"[{c.id}] {c.content}")   # exactly today's format
    return "\n\n".join(lines)


def retrieve_node(state: State) -> dict:
    return {"context": retrieve(state["question"])}   # no logic change


def generate_node(state: State) -> dict:
    ctx = render_context(state["context"])            # was an inline comprehension
    with span("generate", **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "chat"}):
        answer = chat(
            [
                {
                    "role": "system",
                    # Keep [id] citation; ADD: name the section/page in prose when present.
                    "content": (
                        "Answer using only the provided context. Cite [id]s. "
                        "When a context line names a section or page in parentheses, "
                        "also name it in prose, e.g. (see \"Mature AI Stacks > "
                        "Observability\"). If the context is insufficient, say so."
                    ),
                },
                {"role": "user", "content": f"Context:\n{ctx}\n\nQuestion: {state['question']}"},
            ]
        )
    return {"answer": answer}
