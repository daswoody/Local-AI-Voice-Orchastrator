"""Agenten-System (4.16): Admin-CRUD, Tool-Anbindung an den Agent-Loop und
Ausfuehrung als eigener LiteLLM-Lauf mit eigenem Modell/Prompt."""

import json
from unittest.mock import AsyncMock

from orchestrator import graph as graph_module, repos
from orchestrator.services import mcp_gateway as mcp_gateway_module


def _open(ws) -> None:
    assert ws.receive_json()["type"] == "session"


def _collect_until_done(ws) -> list[dict]:
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == "done":
            return frames


def _tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


# ---- Admin-CRUD ---------------------------------------------------------------


def test_agent_crud_roundtrip(client, admin_headers):
    created = client.post(
        "/v1/admin/agents",
        json={"slug": "websuche", "name": "Websuche-Agent",
              "description": "Recherchiert im Web.",
              "system_prompt": "Du recherchierst.", "model": "mistral/large"},
        headers=admin_headers,
    )
    assert created.status_code == 201
    assert created.json()["enabled"] is True

    listed = client.get("/v1/admin/agents", headers=admin_headers).json()
    assert [a["slug"] for a in listed] == ["websuche"]

    updated = client.put(
        "/v1/admin/agents/websuche",
        json={"slug": "websuche", "name": "Websuche", "description": "Neu.",
              "system_prompt": "", "model": "openai/gpt", "enabled": False},
        headers=admin_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["model"] == "openai/gpt"
    assert updated.json()["enabled"] is False

    assert client.delete("/v1/admin/agents/websuche", headers=admin_headers).status_code == 204
    assert client.get("/v1/admin/agents", headers=admin_headers).json() == []


def test_agent_slug_is_validated_and_unique(client, admin_headers):
    payload = {"slug": "Web Suche!", "name": "x", "description": "y", "model": "m"}
    assert client.post("/v1/admin/agents", json=payload, headers=admin_headers).status_code == 422

    ok = {"slug": "coding", "name": "Coding", "description": "Schreibt Code.", "model": "m"}
    assert client.post("/v1/admin/agents", json=ok, headers=admin_headers).status_code == 201
    assert client.post("/v1/admin/agents", json=ok, headers=admin_headers).status_code == 409


def test_agent_routes_require_admin(client):
    assert client.get("/v1/admin/agents").status_code == 401


# ---- Tool-Anbindung -------------------------------------------------------------


def test_enabled_agents_appear_as_tools(client, monkeypatch):
    """Aktive Agenten erscheinen dem Haupt-LLM als Tool agent-<slug> mit der
    Admin-Beschreibung; deaktivierte nicht."""
    repos.create_agent("websuche", "Websuche", "Recherchiert aktuelle Infos im Web.",
                       "", "cloud/search", enabled=True)
    repos.create_agent("coding", "Coding", "Schreibt Code.", "", "cloud/code", enabled=False)

    offered = {}

    async def capture_chat(messages, tools=None, model=None):
        offered["tools"] = tools or []
        return {"role": "assistant", "content": "Ok."}

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", capture_chat)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Hallo"})
        _collect_until_done(ws)

    names = [t["function"]["name"] for t in offered["tools"]]
    assert "agent-websuche" in names
    assert "agent-coding" not in names
    agent_tool = next(t for t in offered["tools"] if t["function"]["name"] == "agent-websuche")
    assert "Recherchiert aktuelle Infos im Web." in agent_tool["function"]["description"]
    assert agent_tool["function"]["parameters"]["required"] == ["task"]


def test_agent_dispatch_runs_own_llm_with_own_model_and_prompt(client, monkeypatch):
    """Haupt-LLM delegiert an agent-websuche -> eigener LiteLLM-Lauf mit dem
    Modell und System-Prompt des Agenten; Ergebnis kommt als Tool-Message
    zurueck in den Haupt-Loop."""
    repos.create_agent("websuche", "Websuche", "Recherchiert im Web.",
                       "Du bist ein Recherche-Agent.", "cloud/search")

    calls = []

    async def fake_chat(messages, tools=None, model=None):
        calls.append({"messages": [dict(m) for m in messages], "tools": tools, "model": model})
        if model == "cloud/search":
            return {"role": "assistant", "content": "Ergebnis: Es regnet morgen."}
        if len(calls) == 1:
            return _tool_call("agent-websuche", {"task": "Wetter morgen recherchieren"})
        return {"role": "assistant", "content": "Morgen regnet es."}

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", fake_chat)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Wie wird das Wetter morgen?"})
        frames = _collect_until_done(ws)

    assert {"type": "assistant_text", "text": "Morgen regnet es.", "final": True} in frames

    agent_calls = [c for c in calls if c["model"] == "cloud/search"]
    assert len(agent_calls) == 1
    assert agent_calls[0]["messages"][0] == {
        "role": "system", "content": "Du bist ein Recherche-Agent."}
    assert agent_calls[0]["messages"][1] == {
        "role": "user", "content": "Wetter morgen recherchieren"}

    # Haupt-Loop hat das Agent-Ergebnis als Tool-Message gesehen
    main_final = calls[-1]
    tool_messages = [m for m in main_final["messages"] if m["role"] == "tool"]
    assert tool_messages[0]["content"] == "Ergebnis: Es regnet morgen."


def test_agent_can_use_server_tools(client, monkeypatch):
    """Der Agent bekommt die MCP-Server-Tools und darf sie in einer eigenen
    Schleife nutzen (z. B. Websuche-Agent -> Such-Tool)."""
    repos.create_agent("websuche", "Websuche", "Recherchiert im Web.", "", "cloud/search")
    monkeypatch.setattr(
        mcp_gateway_module.mcp_gateway, "list_openai_tools",
        AsyncMock(return_value=[{
            "type": "function",
            "function": {"name": "Search-query", "description": "Websuche",
                         "parameters": {"properties": {"q": {"type": "string"}}}},
        }]),
    )
    mcp_call = AsyncMock(return_value="Treffer: morgen Regen")
    monkeypatch.setattr(mcp_gateway_module.mcp_gateway, "call_tool", mcp_call)

    agent_rounds = {"count": 0}

    async def fake_chat(messages, tools=None, model=None):
        if model == "cloud/search":
            agent_rounds["count"] += 1
            # Agent-Schleife: Schemas muessen auch hier normalisiert sein
            if tools:
                for tool in tools:
                    assert tool["function"]["parameters"]["type"] == "object"
            if agent_rounds["count"] == 1:
                return _tool_call("Search-query", {"q": "wetter morgen"})
            return {"role": "assistant", "content": "Morgen Regen (Quelle: Suche)."}
        if not any(m["role"] == "tool" for m in messages):
            return _tool_call("agent-websuche", {"task": "Wetter recherchieren"})
        return {"role": "assistant", "content": "Fertig."}

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", fake_chat)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Wetter?"})
        frames = _collect_until_done(ws)

    assert {"type": "assistant_text", "text": "Fertig.", "final": True} in frames
    mcp_call.assert_awaited_once_with("Search-query", {"q": "wetter morgen"})


def test_agent_failure_becomes_tool_error_not_turn_abort(client, monkeypatch):
    """Stuerzt der Agenten-Lauf ab (z. B. Cloud-Modell nicht erreichbar),
    wird das zum Tool-Fehler-Ergebnis - der Turn stirbt nicht."""
    repos.create_agent("websuche", "Websuche", "Recherchiert im Web.", "", "cloud/search")

    transcript = []

    async def fake_chat(messages, tools=None, model=None):
        if model == "cloud/search":
            raise RuntimeError("LLM-Fehler 401 (Modell cloud/search): kein API-Key")
        transcript.append([dict(m) for m in messages])
        if len(transcript) == 1:
            return _tool_call("agent-websuche", {"task": "recherchiere"})
        return {"role": "assistant", "content": "Die Websuche ist gerade nicht verfuegbar."}

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", fake_chat)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Such mal"})
        frames = _collect_until_done(ws)

    assert not [f for f in frames if f["type"] == "error"]
    tool_messages = [m for m in transcript[-1] if m["role"] == "tool"]
    assert "Tool-Fehler bei agent-websuche" in tool_messages[0]["content"]
    assert "kein API-Key" in tool_messages[0]["content"]
