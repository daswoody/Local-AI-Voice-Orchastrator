"""MCP-Client gegen LiteLLMs Gateway (4.6, Mikro-Phase 1.12).

LiteLLM aggregiert alle registrierten MCP-Server (mcp-time, spaeter
Kalender/Notes/Paperless aus 1.13) unter einem Endpoint und prefixt die
Tool-Namen mit dem Server-Namen (z. B. Time-current_time). Der
Orchestrator ist hier reiner MCP-Client ueber Streamable HTTP; Auth per
x-litellm-api-key (LiteLLM-Konvention).

Pro Aufruf wird eine frische Session geoeffnet - bei Heim-Last voellig
ausreichend und robuster als eine langlebige Verbindung."""

import logging
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..config import settings

logger = logging.getLogger(__name__)


class McpGateway:
    def __init__(self) -> None:
        self._tools_cache: list[dict] = []
        self._cache_time: float = 0.0

    def _headers(self) -> dict[str, str]:
        return {
            "x-litellm-api-key": f"Bearer {settings.litellm_api_key}",
            "Authorization": f"Bearer {settings.litellm_api_key}",
        }

    async def list_openai_tools(self) -> list[dict]:
        """Tools des Gateways im OpenAI-Function-Format, gecacht.

        Nicht erreichbares Gateway ist KEIN Fehler: dann gibt es in diesem
        Turn eben keine Server-Tools (Geraete-Tools und show_card
        funktionieren unabhaengig davon)."""
        if self._tools_cache and time.monotonic() - self._cache_time < settings.mcp_tools_cache_seconds:
            return self._tools_cache

        try:
            async with streamablehttp_client(settings.litellm_mcp_url, headers=self._headers()) as (
                read, write, _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
        except Exception:
            logger.warning("MCP-Gateway nicht erreichbar - Turn laeuft ohne Server-Tools")
            return self._tools_cache  # ggf. veralteter Cache ist besser als nichts

        self._tools_cache = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                },
            }
            for tool in listed.tools
        ]
        self._cache_time = time.monotonic()
        return self._tools_cache

    async def call_tool(self, name: str, arguments: dict) -> str:
        async with streamablehttp_client(settings.litellm_mcp_url, headers=self._headers()) as (
            read, write, _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments=arguments)

        texts = [block.text for block in result.content if getattr(block, "text", None)]
        payload = "\n".join(texts) if texts else ""
        if result.isError:
            return f"Tool-Fehler: {payload or 'unbekannt'}"
        return payload or "(leeres Ergebnis)"


mcp_gateway = McpGateway()
