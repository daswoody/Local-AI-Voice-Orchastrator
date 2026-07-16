"""Agenten-Ausfuehrung (4.16).

Ein Agent ist ein in sich geschlossener LLM-Lauf ueber LiteLLM (4.6) mit
eigenem Modell und System-Prompt. Er bekommt die Server-Tools (MCP-Gateway)
in einer eigenen kleinen Tool-Schleife angeboten - aber KEINE Geraete-Tools
und kein show_card: die sind session-gebunden und bleiben Sache des
Haupt-LLMs, das die Agent-Antwort als Tool-Ergebnis erhaelt."""

import json
import logging
import re

from ..config import settings
from .litellm_client import litellm_client
from .mcp_gateway import mcp_gateway

logger = logging.getLogger(__name__)


async def run_agent(agent: dict, task: str) -> str:
    """Fuehrt die Aufgabe mit Modell/Prompt des Agenten aus und liefert die
    finale Text-Antwort. Fehler duerfen nach oben - der ToolExecutor packt
    sie in ein Tool-Ergebnis, das das Haupt-LLM dem Nutzer erklaeren kann."""
    messages: list[dict] = [
        {"role": "system", "content": agent["system_prompt"]
         or f"Du bist der Spezial-Agent '{agent['name']}'. Erledige die Aufgabe praezise."},
        {"role": "user", "content": task},
    ]

    return await _run_loop(agent, messages)


async def generate_card_html(agent: dict, card_type: str, title: str | None,
                             data: dict) -> str:
    """Layout-Auftrag an den Karten-Agenten (code-card, 4.12 v1.12.2):
    show_card delegiert hierher, wenn das Haupt-LLM kein fertiges HTML
    liefert. Liefert das reine HTML-Fragment (Markdown-Zaeune entfernt)
    oder wirft, wenn der Agent kein brauchbares HTML produziert."""
    task = (
        "Schreibe ein eigenstaendiges HTML-Fragment fuer eine Chat-Karte "
        "(kompakt, max. ca. 400px breit, Inline-CSS, lesbar auf dunklem UND "
        "hellem Hintergrund).\n"
        f"Gewuenschter Kartentyp/Kontext: {card_type}\n"
        f"Titel: {title or '(keiner)'}\n"
        f"Anzuzeigende Daten (JSON): {json.dumps(data, ensure_ascii=False)}\n"
        "Stelle ALLE Daten huebsch dar. Antworte AUSSCHLIESSLICH mit dem "
        "HTML-Fragment - keine Erklaerungen, kein Markdown, keine "
        "<html>/<head>/<body>-Huelle."
    )
    messages = [
        {"role": "system", "content": agent["system_prompt"]
         or "Du bist ein praeziser HTML/CSS-Layouter fuer kleine UI-Karten."},
        {"role": "user", "content": task},
    ]
    # Bewusst OHNE Tool-Schleife: Layout schreiben braucht keine Tools,
    # und ohne Tools kann auch kein strikter Validator dazwischenfunken.
    message = await litellm_client.chat_message(messages, model=agent["model"])
    html = _strip_markdown_fences(message.get("content") or "")
    if "<" not in html:
        raise ValueError(f"Karten-Agent lieferte kein HTML: {html[:120]!r}")
    return html


def _strip_markdown_fences(text: str) -> str:
    """Modelle packen Code trotz Verbot gern in ```html-Zaeune."""
    match = re.search(r"```(?:html)?\s*(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


async def _run_loop(agent: dict, messages: list[dict]) -> str:
    from .tool_executor import _ensure_object_schema

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
