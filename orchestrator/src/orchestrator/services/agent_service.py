"""Agenten-Ausfuehrung (4.16).

Ein Agent ist ein in sich geschlossener LLM-Lauf ueber LiteLLM (4.6) mit
eigenem Modell und System-Prompt. Er bekommt die Server-Tools (MCP-Gateway)
in einer eigenen kleinen Tool-Schleife angeboten - aber KEINE Geraete-Tools
und kein show_card: die sind session-gebunden und bleiben Sache des
Haupt-LLMs, das die Agent-Antwort als Tool-Ergebnis erhaelt."""

import json
import logging

from ..config import settings
from .litellm_client import litellm_client
from .mcp_gateway import mcp_gateway

logger = logging.getLogger(__name__)


async def run_agent(agent: dict, task: str) -> str:
    """Fuehrt die Aufgabe mit Modell/Prompt des Agenten aus und liefert die
    finale Text-Antwort. Fehler duerfen nach oben - der ToolExecutor packt
    sie in ein Tool-Ergebnis, das das Haupt-LLM dem Nutzer erklaeren kann."""
    from .tool_executor import _ensure_object_schema

    messages: list[dict] = [
        {"role": "system", "content": agent["system_prompt"]
         or f"Du bist der Spezial-Agent '{agent['name']}'. Erledige die Aufgabe praezise."},
        {"role": "user", "content": task},
    ]

    for iteration in range(settings.tool_max_iterations + 1):
        tools = None
        if iteration < settings.tool_max_iterations:
            tools = await mcp_gateway.list_openai_tools() or None
            if tools:
                # Gleiche Normalisierung wie im Haupt-Loop: LM Studio & Co.
                # validieren Tool-Schemas strikt (Root type: object).
                for tool in tools:
                    function = tool.get("function") or {}
                    function["parameters"] = _ensure_object_schema(function.get("parameters"))

        message = await litellm_client.chat_message(messages, tools, model=agent["model"])
        messages.append(message)

        tool_calls = message.get("tool_calls") or []
        if not tool_calls or tools is None:
            return message.get("content") or ""

        for tool_call in tool_calls:
            name = tool_call["function"]["name"]
            try:
                arguments = json.loads(tool_call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            try:
                result = await mcp_gateway.call_tool(name, arguments)
            except Exception as exc:
                logger.exception("Agent %s: Tool %s fehlgeschlagen", agent["slug"], name)
                result = f"Tool-Fehler bei {name}: {str(exc)[:200]}"
            messages.append(
                {"role": "tool", "tool_call_id": tool_call.get("id", name), "content": result}
            )

    return messages[-1].get("content") or ""
