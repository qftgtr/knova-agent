from __future__ import annotations

from functools import lru_cache
from typing import TypedDict

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from openai import OpenAI

from .settings import Settings


class GraphState(TypedDict):
    text: str
    reply: str


@lru_cache(maxsize=1)
def _get_openai_client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key)


def _call_llm(text: str, settings: Settings) -> str:
    if settings.ai_model_provider != "openai":
        raise RuntimeError(f"Unsupported provider {settings.ai_model_provider}")

    if not settings.ai_model_key_openai:
        raise RuntimeError("Missing AI_MODEL_KEY_OPENAI")

    client = _get_openai_client(settings.ai_model_key_openai)
    response = client.chat.completions.create(
        model=settings.ai_model_name,
        messages=[{"role": "user", "content": text}],
    )

    message = response.choices[0].message.content if response.choices else ""
    return message or ""


def _build_checkpointer(database_url: str | None):
    if not database_url:
        return MemorySaver()

    try:
        from langgraph.checkpoint.postgres import PostgresSaver

        if hasattr(PostgresSaver, "from_conn_string"):
            return PostgresSaver.from_conn_string(database_url)

        return PostgresSaver(database_url)
    except Exception as exc:
        print(f"Postgres checkpointer disabled: {exc}")
        return MemorySaver()


def build_graph(settings: Settings):
    graph = StateGraph(GraphState)

    def call_llm(state: GraphState) -> dict[str, str]:
        reply = _call_llm(state["text"], settings)
        return {"reply": reply}

    graph.add_node("call_llm", call_llm)
    graph.set_entry_point("call_llm")
    graph.add_edge("call_llm", END)

    checkpointer = _build_checkpointer(settings.database_url)
    return graph.compile(checkpointer=checkpointer)


def run_graph(graph, text: str, thread_id: str) -> str:
    result = graph.invoke({"text": text}, config={"configurable": {"thread_id": thread_id}})
    return result.get("reply", "")
