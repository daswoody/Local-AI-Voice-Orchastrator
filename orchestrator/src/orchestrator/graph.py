from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .services.litellm_client import litellm_client
from .services.weaviate_client import weaviate_client

_SYSTEM_PROMPT = "Du bist eine hilfreiche, deutschsprachige Heim-Assistenz."


class OrchestratorState(TypedDict):
    text: str
    tier: int
    # Effektiver Charakter-Prompt (4.14: global, pro User ueberschreibbar);
    # leer -> Fallback auf den Default oben.
    system_prompt: str
    context_chunks: list[str]
    response: str


async def retrieve_node(state: OrchestratorState) -> OrchestratorState:
    chunks = await weaviate_client.search(state["text"], tier=state["tier"])
    return {**state, "context_chunks": chunks}


async def generate_node(state: OrchestratorState) -> OrchestratorState:
    response = await litellm_client.chat(_build_messages(state))
    return {**state, "response": response}


def _build_messages(state: OrchestratorState) -> list[dict[str, str]]:
    system_prompt = state.get("system_prompt") or _SYSTEM_PROMPT
    if state["context_chunks"]:
        context = "\n".join(f"- {chunk}" for chunk in state["context_chunks"])
        system_prompt += f"\n\nRelevanter Kontext:\n{context}"
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": state["text"]},
    ]


def build_graph():
    graph = StateGraph(OrchestratorState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("generate", generate_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", END)
    return graph.compile()


orchestrator_graph = build_graph()
