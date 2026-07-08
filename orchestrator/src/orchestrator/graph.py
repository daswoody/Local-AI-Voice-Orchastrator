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
    # Bild-Eingabe (Phase 2.5/3: Screenshot-Analyse) als data-URL
    # ("data:image/png;base64,..."); leer = reiner Text-Turn. Geht als
    # multimodaler content-Teil an LiteLLM (OpenAI-Format), das Modell
    # dahinter muss Vision koennen (Gemma 4 E4B: ja).
    image_data_url: str
    context_chunks: list[str]
    # Vollstaendiger Nachrichtenverlauf des Turns inkl. assistant-Messages
    # mit tool_calls und deren tool-Ergebnissen (OpenAI-Format).
    messages: list[dict]
    # ToolExecutor der Session (None -> Turn laeuft ohne Tools).
    executor: Any
    iterations: int
    response: str


async def retrieve_node(state: OrchestratorState) -> OrchestratorState:
    # Reine Bild-Turns ("was siehst du?") haben keinen sinnvollen
    # Suchbegriff - RAG dann ueberspringen.
    if not state["text"].strip():
        return {**state, "context_chunks": []}
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

        # Bild-Ergebnisse (z. B. capture_screenshot der Windows-App): das
        # OpenAI-Format erlaubt keine Bilder in tool-Messages, und Base64
        # als Text wuerde den Kontext sprengen. Deshalb: kurzes
        # Text-Ergebnis in die tool-Message, das Bild selbst als
        # multimodale user-Message dahinter - so "sieht" das LLM den
        # Screenshot in der naechsten Agent-Runde.
        image_data_url = _extract_image_data_url(result)
        if image_data_url is not None:
            messages.append(
                {"role": "tool", "tool_call_id": tool_call.get("id", name),
                 "content": "Bild aufgenommen, es folgt als Anhang der naechsten Nachricht."}
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"[Bild vom Geraete-Tool {name}]"},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }
            )
            continue

        messages.append(
            {"role": "tool", "tool_call_id": tool_call.get("id", name), "content": result}
        )
    return {**state, "messages": messages}


def _extract_image_data_url(result: str) -> str | None:
    """Erkennt {"image_b64": "...", "mime": "image/png"} in Tool-Ergebnissen
    (Konvention der Geraete-Tool-Bridge fuer Bild-Antworten, 4.13)."""
    if '"image_b64"' not in result:
        return None
    try:
        payload = json.loads(result)
        image_b64 = payload.get("image_b64")
        if not image_b64:
            return None
        mime = payload.get("mime") or "image/png"
        return f"data:{mime};base64,{image_b64}"
    except (json.JSONDecodeError, AttributeError):
        return None


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

    if state.get("image_data_url"):
        # Multimodale user-Message (OpenAI-Format): Text + Bild.
        text = state["text"].strip() or "Beschreibe, was du auf dem Bild siehst."
        user_content: Any = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": state["image_data_url"]}},
        ]
    else:
        user_content = state["text"]

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def initial_state(
    text: str,
    tier: int,
    system_prompt: str = "",
    executor: Any = None,
    image_data_url: str = "",
) -> OrchestratorState:
    return {
        "text": text,
        "tier": tier,
        "system_prompt": system_prompt,
        "image_data_url": image_data_url,
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
