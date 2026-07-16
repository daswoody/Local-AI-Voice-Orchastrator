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
_AGENT_PREFIX = "agent-"
# Reservierter Agent-Slug (4.16/4.12): Existiert ein aktiver Agent
# "code-card", schreibt ER die HTML-Layouts fuer Karten - show_card
# delegiert automatisch, wenn das Haupt-LLM kein fertiges HTML liefert.
# So muss ein kleines lokales Modell kein HTML in Tool-Argumente stopfen.
_CARD_AGENT_SLUG = "code-card"


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


def _card_agent() -> dict | None:
    agent = repos.get_agent(_CARD_AGENT_SLUG)
    return agent if agent is not None and agent["enabled"] else None


def _show_card_schema() -> dict:
    card_types = [entry["card_type"] for entry in repos.list_card_layouts(0)]
    if _card_agent() is not None:
        layout_hint = (
            "Passt kein vorhandener Kartentyp, nutze card_type 'html' und "
            "lege ALLE anzuzeigenden Werte in data - das eigentliche "
            "HTML-Layout schreibt dann automatisch der Agent code-card, du "
            "musst KEIN html-Feld liefern. "
        )
    else:
        layout_hint = (
            "Passt kein vorhandener Kartentyp, erstelle selbst ein Layout: "
            "card_type 'html' und im Feld html ein eigenstaendiges "
            "HTML-Fragment mit Inline-CSS. "
        )
    return {
        "type": "function",
        "function": {
            "name": _SHOW_CARD_NAME,
            "description": (
                "Zeigt dem Nutzer parallel zur Antwort eine Karte in der App an. "
                "Nutze das fuer strukturierte Inhalte (Listen, Termine, Wetter, "
                f"Zusammenfassungen). Verfuegbare Kartentypen: {', '.join(card_types)}. "
                f"{layout_hint}"
                "WICHTIG: Gib HTML ausschliesslich im html-Feld an und wiederhole "
                "es NIE in deiner Text-Antwort - der Nutzer sieht die Karte direkt, "
                "HTML im Antworttext wird als roher Code angezeigt. "
                "Unbekannte Typen rendert die App als generic-Karte "
                "(data.headline + data.body)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_type": {"type": "string", "description": "Kartentyp, z. B. generic oder html"},
                    "title": {"type": "string", "description": "Optionaler Titel der Karte"},
                    "data": {"type": "object", "description": "Frei strukturierte Daten passend zum Kartentyp"},
                    "html": {"type": "string", "description": "Nur bei card_type 'html': selbst geschriebenes HTML-Fragment fuer die Karte"},
                },
                "required": ["card_type", "data"],
            },
        },
    }


def _agent_tool_schema(agent: dict) -> dict:
    """Agenten (4.16) erscheinen dem Haupt-LLM als normale Tools - die
    Auswahl laeuft rein ueber die Admin-Beschreibung, ohne Sonder-Routing."""
    return {
        "type": "function",
        "function": {
            "name": f"{_AGENT_PREFIX}{agent['slug']}",
            "description": (
                f"Delegiert eine Aufgabe an den Spezial-Agenten '{agent['name']}': "
                f"{agent['description']}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Die Aufgabe fuer den Agenten, vollstaendig und in eigenen Worten (der Agent kennt das Gespraech nicht)",
                    },
                },
                "required": ["task"],
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
        # Aktivitaets-Hook (v1.12.1): meldet der Session Start/Ende jedes
        # Tool-/Agenten-Aufrufs (status: running/done/error), damit der
        # Client anzeigen kann, was die KI gerade tut.
        on_activity: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._device_tools = {tool.name: tool for tool in (device_tools or [])}
        self._device_call = device_call
        self._card_push = card_push
        self._on_tool_start = on_tool_start
        self._on_activity = on_activity

    async def list_openai_tools(self) -> list[dict]:
        tools: list[dict] = []
        if self._card_push is not None:
            tools.append(_show_card_schema())
        for agent in repos.list_agents(enabled_only=True):
            tools.append(_agent_tool_schema(agent))
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
        # show_card ist rein visuell und quasi-instant: weder Filler noch
        # Aktivitaets-Anzeige - die Karte selbst IST die Anzeige.
        visible = name != _SHOW_CARD_NAME
        await self._notify_activity(visible, name, "running")
        # Tool als Task starten, damit der Filler-Hook seinen Delay dagegen
        # rennen lassen kann: schnelles Tool -> Filler entfaellt (1.7d).
        pending = asyncio.create_task(self._dispatch(name, arguments))
        if self._on_tool_start is not None and visible:
            await self._on_tool_start(name, pending)
        result = await pending
        await self._notify_activity(
            visible, name, "error" if result.startswith("Tool-Fehler") else "done"
        )
        return result

    async def _notify_activity(self, visible: bool, name: str, status: str) -> None:
        # Anzeige ist Komfort: ein kaputter Hook darf kein Tool-Ergebnis kosten.
        if self._on_activity is None or not visible:
            return
        try:
            await self._on_activity(name, status)
        except Exception:
            logger.exception("Aktivitaets-Hook fehlgeschlagen (Tool %s)", name)

    async def _dispatch(self, name: str, arguments: dict) -> str:
        try:
            if name == _SHOW_CARD_NAME and self._card_push is not None:
                return await self._execute_show_card(arguments)
            if name.startswith(_AGENT_PREFIX):
                agent = repos.get_agent(name[len(_AGENT_PREFIX):])
                if agent is not None and agent["enabled"]:
                    from .agent_service import run_agent

                    return await run_agent(agent, str(arguments.get("task") or ""))
            if name in self._device_tools and self._device_call is not None:
                return await self._device_call(name, arguments)
            return await mcp_gateway.call_tool(name, arguments)
        except Exception as exc:
            logger.exception("Tool %s fehlgeschlagen", name)
            return f"Tool-Fehler bei {name}: {str(exc)[:200]}"

    async def _execute_show_card(self, arguments: dict) -> str:
        # CardEnvelope nach 4.12: {type, version, title?, data}
        data = arguments.get("data") or {}
        card_type = str(arguments.get("card_type", "generic"))
        # Modelle liefern data gern als JSON-String oder stopfen das HTML
        # direkt hinein - tolerant einsammeln statt leere Karten pushen.
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                data = parsed
            elif "<" in data:
                data = {"html": data}
            else:
                data = {"body": data}
        title = str(arguments["title"]) if arguments.get("title") else None
        # KI-geschriebene Ad-hoc-HTML-Karte (4.12 v1.12): HTML aus dem
        # html-Feld ODER aus data.html; der Typ ist dann immer "html"
        # (Alt-Clients rendern die generic-Karte, die Web-UI ein
        # sandboxed iframe).
        html = arguments.get("html") or data.get("html")
        if not html and self._card_needs_generated_html(card_type, data):
            # Kein fertiges HTML da, aber ohne HTML gaebe es nur eine leere
            # Karte -> Layout vom Karten-Agenten (code-card) schreiben
            # lassen (v1.12.2). Das entlastet das Haupt-LLM: es liefert nur
            # Titel + Daten, der Agent (z. B. Cloud-Modell) baut das HTML.
            html = await self._generate_card_html(card_type, title, data)
            if html is None:
                return json.dumps({
                    "ok": False,
                    "fehler": "Fuer diese Karte fehlt darstellbarer Inhalt: "
                              "liefere das html-Feld mit einem HTML-Fragment "
                              "(NUR dort, nicht im Antworttext) oder nutze "
                              "data.headline/data.body. Hinweis fuer den "
                              "Admin: Ein aktiver Agent 'code-card' wuerde "
                              "solche Layouts automatisch schreiben.",
                })
            if html.startswith("Tool-Fehler"):
                return html
        if html:
            card_type = "html"
            data = {**data, "html": str(html)}
        envelope = {
            "type": card_type,
            "version": 1,
            "data": data,
        }
        if title:
            envelope["title"] = title
        await self._card_push(envelope)
        return json.dumps({"ok": True, "angezeigt": envelope["type"]})

    @staticmethod
    def _card_needs_generated_html(card_type: str, data: dict) -> bool:
        """HTML muss erzeugt werden, wenn explizit 'html' angefordert wurde
        oder der Kartentyp kein gespeichertes Layout hat UND die Daten auch
        das generic-Fallback (headline/body) nicht fuellen wuerden."""
        if card_type == "html":
            return True
        known_types = {entry["card_type"] for entry in repos.list_card_layouts(0)}
        if card_type in known_types:
            return False
        return not (data.get("headline") or data.get("body"))

    async def _generate_card_html(self, card_type: str, title: str | None,
                                  data: dict) -> str | None:
        """Laesst den Karten-Agenten (code-card) das HTML schreiben.
        None -> kein Agent konfiguriert; "Tool-Fehler ..." -> Agent-Lauf
        fehlgeschlagen (geht als korrigierbares Ergebnis ans Haupt-LLM)."""
        agent = _card_agent()
        if agent is None:
            return None
        from .agent_service import generate_card_html

        agent_tool = f"{_AGENT_PREFIX}{agent['slug']}"
        await self._notify_activity(True, agent_tool, "running")
        try:
            html = await generate_card_html(agent, card_type, title, data)
        except Exception as exc:
            logger.exception("Karten-Agent %s fehlgeschlagen", agent["slug"])
            await self._notify_activity(True, agent_tool, "error")
            return f"Tool-Fehler bei {agent_tool}: {str(exc)[:200]}"
        await self._notify_activity(True, agent_tool, "done")
        return html
