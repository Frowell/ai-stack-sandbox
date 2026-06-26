"""Orchestration with LangGraph.

A two-node graph: retrieve -> generate. Real systems fan this out to 3-8
specialist nodes with checkpointed, durable state; this is the smallest honest
version that still shows the pattern. Run directly:  python -m app.agent "..."
"""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .gateway import chat
from .observability import span
from .retrieval import retrieve


class State(TypedDict):
    question: str
    context: list[tuple[int, str]]
    answer: str


def retrieve_node(state: State) -> dict:
    return {"context": retrieve(state["question"])}


def generate_node(state: State) -> dict:
    ctx = "\n\n".join(f"[{doc_id}] {content}" for doc_id, content in state["context"])
    with span("generate", **{"gen_ai.operation.name": "chat", "gen_ai.request.model": "chat"}):
        answer = chat(
            [
                {
                    "role": "system",
                    "content": "Answer using only the provided context. Cite [id]s. If the context is insufficient, say so.",
                },
                {"role": "user", "content": f"Context:\n{ctx}\n\nQuestion: {state['question']}"},
            ]
        )
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


def ask(question: str) -> str:
    with span("agent.run", **{"gen_ai.operation.name": "agent", "input.question": question}):
        return GRAPH.invoke({"question": question})["answer"]


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "What makes an AI stack 'mature'?"
    print(ask(q))
