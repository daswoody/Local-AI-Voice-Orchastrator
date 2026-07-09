"""Tool-Routing fuer den Agent-Loop (Mikro-Phase 1.12).

Drei Tool-Quellen, eine Schnittstelle:
- show_card (builtin): pusht eine Karte (4.12) ueber die WebSocket-Verbindung
- Geraete-Tools: aus dem hello-Manifest der App, session-gebunden (4.6/4.13),
  Ausfuehrung laeuft als tool_call/tool_result-Roundtrip zurueck zur App
- Server-Tools: alles, was LiteLLMs MCP-Gateway aggregiert (mcp-time, ...)

Der Executor gehoert der jeweiligen Session (Geraete-Tools und Karten sind
verbindungsspezifisch) und wird dem LangGraph-Flow per State mitgegeben."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from .. import repos
from ..schemas import DeviceTool
from .mcp_gateway import mcp_gateway

logger = logging.getLogger(__name__)

_SHOW_CARD_NAME = "show_card"


def _ensure_object_schema(schema) -> dict:
    """Normalisiert ein Tool-Parameter-Schema auf die strikte
    OpenAI-/LM-Studio-Erwartung: Root ist IMMER {"type": "object"} mit
    properties-Dict. Fremdschluessel wie $schema fliegen raus - sie sind
    fuer die Funktion bedeutungslos, koennen strikte Validatoren aber
    stolpern lassen."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    normalized = {key: value for key, value in schema.items() if key != "$schema"}
    normalized["type"] = "object"
    if not isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {}
    return normalized


def _show_card_schema() -> dict:
    card_types = [entry["card_type"] for entry in repos.list_card_layouts(0)]
    return {
        "type": "function",
        "function": {
            "name": _SHOW_CARD_NAME,
            "description": (
                "Zeigt dem Nutzer parallel zur Antwort eine Karte in der App an. "
                "Nutze das fuer strukturierte Inhalte (Listen, Termine, Wetter, "
                f"Zusammenfassungen). Verfuegbare Kartentypen: {', '.join(card_types)}. "
                "Unbekannte Typen rendert die App als generic-Karte "
                "(data.headline + data.body)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_type": {"type": "string", "description": "Kartentyp, z. B. generic"},
                    "title": {"type": "string", "description": "Optionaler Titel der Karte"},
                    "data": {"type": "object", "description": "Frei strukturierte Daten passend zum Kartentyp"},
                },
                "required": ["card_type", "data"],
            },
        },
    }


class ToolExecutor:
    def __init__(
        self,
        device_tools: list[DeviceTool] | None = None,
        device_call: Callable[[str, dict], Awaitable[str]] | None = None,
        card_push: Callable[[dict], Awaitable[None]] | None = None,
        # Filler-Hook (1.7d): bekommt Tool-Namen UND den laufenden Tool-Task,
        # damit die Session den Filler-Delay gegen das Tool rennen lassen
        # kann (schnelles Tool -> kein Filler).
        on_tool_start: Callable[[str, "asyncio.Task"], Awaitable[None]] | None = None,
    ) -> None:
        self._device_tools = {tool.name: tool for tool in (device_tools or [])}
        self._device_call = device_call
        self._card_push = card_push
        self._on_tool_start = on_tool_start

    async def list_openai_tools(self) -> list[dict]:
        tools: list[dict] = []
        if self._card_push is not None:
            tools.append(_show_card_schema())
        for tool in self._device_tools.values():
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or f"Geraete-Tool {tool.name}",
                        "parameters": tool.parameters or {"type": "object", "properties": {}},
                    },
                }
            )
        tools.extend(await mcp_gateway.list_openai_tools())
        # Alle Parameter-Schemas normalisieren, egal aus welcher Quelle:
        # LM Studio validiert Requests strikt (Zod) und verlangt am
        # Schema-Root "type": "object" - MCP-Server lassen den Root-type
        # gern weg, und schon lehnt LM Studio den GESAMTEN Request mit
        # 400 invalid_union_discriminator ab (Cloud-Anbieter sind
        # toleranter, daher fiel es erst beim lokalen Modell auf).
        for tool in tools:
            function = tool.get("function") or {}
            function["parameters"] = _ensure_object_schema(function.get("parameters"))
        return tools

    async def execute(self, name: str, arguments: dict) -> str:
        """Fuehrt ein Tool aus und liefert IMMER einen String (auch bei
        Fehlern) - der Agent-Loop haengt das als tool-Message an, und das
        LLM kann dem Nutzer erklaeren, was schiefging."""
        # Tool als Task starten, damit der Filler-Hook seinen Delay dagegen
        # rennen lassen kann: schnelles Tool -> Filler entfaellt (1.7d).
        pending = asyncio.create_task(self._dispatch(name, arguments))
        if self._on_tool_start is not None:
            # show_card ist rein visuell und quasi-instant - dafuer keinen
            # gesprochenen Filler anstossen.
            if name != _SHOW_CARD_NAME:
                await self._on_tool_start(name, pending)
        return await pending

    async def _dispatch(self, name: str, arguments: dict) -> str:
        try:
            if name == _SHOW_CARD_NAME and self._card_push is not None:
                return await self._execute_show_card(arguments)
            if name in self._device_tools and self._device_call is not None:
                return await self._device_call(name, arguments)
            return await mcp_gateway.call_tool(name, arguments)
        except Exception as exc:
            logger.exception("Tool %s fehlgeschlagen", name)
            return f"Tool-Fehler bei {name}: {str(exc)[:200]}"

    async def _execute_show_card(self, arguments: dict) -> str:
        # CardEnvelope nach 4.12: {type, version, title?, data}
        envelope = {
            "type": str(arguments.get("card_type", "generic")),
            "version": 1,
            "data": arguments.get("data") or {},
        }
        if arguments.get("title"):
            envelope["title"] = str(arguments["title"])
        await self._card_push(envelope)
        return json.dumps({"ok": True, "angezeigt": envelope["type"]})
