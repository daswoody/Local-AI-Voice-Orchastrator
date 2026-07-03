import json
import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .config import settings
from .services.litellm_client import litellm_client
from .services.weaviate_client import weaviate_client

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = "Du bist eine hilfreiche, deutschsprachige Heim-Assistenz."


class OrchestratorState(TypedDict):
    text: str
    tier: int
    # Effektiver Charakter-Prompt (4.14: global, pro User ueberschreibbar);
    # leer -> Fallback auf den Default oben.
    system_prompt: str
    context_chunks: list[str]
    # Vollstaendiger Nachrichtenverlauf des Turns inkl. assistant-Messages
    # mit tool_calls und deren tool-Ergebnissen (OpenAI-Format).
    messages: list[dict]
    # ToolExecutor der Session (None -> Turn laeuft ohne Tools).
    executor: Any
    iterations: int
    response: str


async def retrieve_node(state: OrchestratorState) -> OrchestratorState:
    chunks = await weaviate_client.search(state["text"], tier=state["tier"])
    return {**state, "context_chunks": chunks}


async def agent_node(state: OrchestratorState) -> OrchestratorState:
    messages = state["messages"] or _initial_messages(state)

    # Ab der Iterations-Obergrenze bewusst OHNE Tools anfragen: Das LLM darf
    # dann nicht weiter werkeln, sondern muss aus den vorliegenden
    # Tool-Ergebnissen eine Antwort formulieren (Endlosschleifen-Schutz).
    tools = None
    if state["executor"] is not None and state["iterations"] < settings.tool_max_iterations:
        tools = await state["executor"].list_openai_tools() or None

    message = await litellm_client.chat_message(messages, tools)
    messages.append(message)

    return {
        **state,
        "messages": messages,
        "iterations": state["iterations"] + 1,
        "response": message.get("content") or "",
    }


async def tools_node(state: OrchestratorState) -> OrchestratorState:
    messages = state["messages"]
    for tool_call in messages[-1].get("tool_calls") or []:
        name = tool_call["function"]["name"]
        try:
            arguments = json.loads(tool_call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        result = await state["executor"].execute(name, arguments)
        messages.append(
            {"role": "tool", "tool_call_id": tool_call.get("id", name), "content": result}
        )
    return {**state, "messages": messages}


def _route_after_agent(state: OrchestratorState) -> str:
    last = state["messages"][-1]
    if (
        last.get("tool_calls")
        and state["executor"] is not None
        # Doppelter Schutz zur tools=None-Logik im agent_node: selbst wenn
        # das LLM ohne Tool-Angebot tool_calls halluziniert, ist Schluss.
        and state["iterations"] <= settings.tool_max_iterations
    ):
        return "tools"
    return END


def _initial_messages(state: OrchestratorState) -> list[dict]:
    system_prompt = state.get("system_prompt") or _SYSTEM_PROMPT
    if state["context_chunks"]:
        context = "\n".join(f"- {chunk}" for chunk in state["context_chunks"])
        system_prompt += f"\n\nRelevanter Kontext:\n{context}"
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": state["text"]},
    ]


def initial_state(
    text: str, tier: int, system_prompt: str = "", executor: Any = None
) -> OrchestratorState:
    return {
        "text": text,
        "tier": tier,
        "system_prompt": system_prompt,
        "context_chunks": [],
        "messages": [],
        "executor": executor,
        "iterations": 0,
        "response": "",
    }


def build_graph():
    graph = StateGraph(OrchestratorState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "agent")
    graph.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


orchestrator_graph = build_graph()
