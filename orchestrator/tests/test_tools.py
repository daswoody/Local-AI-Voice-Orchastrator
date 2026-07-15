"""Tool-Calling im Orchestrator (Mikro-Phase 1.12): Agent-Loop, MCP-Server-
Tools, Geraete-Tool-Bridge, show_card und Tool-Filler. Frame-Felder laut
docs/PROTOCOL.md der App (call_id, tools-Manifest, session-Frame)."""

import json
from unittest.mock import AsyncMock

from orchestrator import graph as graph_module, repos
from orchestrator.audio import pcm_to_b64
from orchestrator.config import settings
from orchestrator.routers import stream as stream_module
from orchestrator.services import filler_service
from orchestrator.services import mcp_gateway as mcp_gateway_module


def _open(ws) -> None:
    assert ws.receive_json()["type"] == "session"


def _next_frame(ws) -> dict:
    """Naechstes inhaltliches Frame; ueberspringt das conversation-Frame
    der zentralen Historie (Phase 2.5)."""
    frame = ws.receive_json()
    if frame["type"] == "conversation":
        frame = ws.receive_json()
    return frame


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


def _scripted_llm(monkeypatch, script: list[dict], transcript: list | None = None):
    """Fake-LLM, das eine feste Antwortfolge abspielt (Agent-Loop-Runden)."""
    responses = list(script)

    async def fake_chat(messages, tools=None):
        if transcript is not None:
            transcript.append({"messages": [dict(m) for m in messages], "tools": tools})
        return responses.pop(0)

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", fake_chat)


def _collect_until_done(ws) -> list[dict]:
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == "done":
            return frames


def test_server_tool_via_mcp_gateway(client, monkeypatch):
    """LLM ruft ein Gateway-Tool (z. B. Time-current_time), Ergebnis fliesst
    in die finale Antwort."""
    transcript = []
    _scripted_llm(monkeypatch, [
        _tool_call("Time-current_time", {"timezone": "Europe/Berlin"}),
        {"role": "assistant", "content": "Es ist 12:00 Uhr."},
    ], transcript)

    mcp_call = AsyncMock(return_value="2026-07-03T12:00:00+02:00")
    monkeypatch.setattr(mcp_gateway_module.mcp_gateway, "call_tool", mcp_call)
    monkeypatch.setattr(
        mcp_gateway_module.mcp_gateway, "list_openai_tools",
        AsyncMock(return_value=[{
            "type": "function",
            "function": {"name": "Time-current_time", "description": "Zeit",
                         "parameters": {"type": "object", "properties": {}}},
        }]),
    )

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Wie spaet ist es?"})
        frames = _collect_until_done(ws)

    assert {"type": "assistant_text", "text": "Es ist 12:00 Uhr.", "final": True} in frames
    mcp_call.assert_awaited_once_with("Time-current_time", {"timezone": "Europe/Berlin"})
    # Zweite LLM-Runde hat das Tool-Ergebnis als tool-Message gesehen
    tool_messages = [m for m in transcript[1]["messages"] if m["role"] == "tool"]
    assert tool_messages[0]["content"] == "2026-07-03T12:00:00+02:00"


def test_device_tool_roundtrip_over_websocket(client, monkeypatch):
    """Geraete-Tool-Bridge (4.13): tool_call an die App, tool_result zurueck.
    Manifest kommt laut PROTOCOL.md im hello-Feld "tools"."""
    _scripted_llm(monkeypatch, [
        _tool_call("set_alarm", {"time": "07:30"}),
        {"role": "assistant", "content": "Wecker fuer 7:30 Uhr gestellt."},
    ])

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({
            "type": "hello", "mode": "chat",
            "tools": [{"name": "set_alarm", "description": "Stellt einen Wecker",
                       "parameters": {"type": "object", "properties": {"time": {"type": "string"}}},
                       "sensitive": False}],
        })
        ws.send_json({"type": "text_input", "text": "Weck mich um 7:30"})

        tool_call = _next_frame(ws)
        assert tool_call["type"] == "tool_call"
        assert tool_call["name"] == "set_alarm"
        assert tool_call["arguments"] == {"time": "07:30"}
        assert tool_call["call_id"]

        ws.send_json({"type": "tool_result", "call_id": tool_call["call_id"],
                      "ok": True, "result": {"alarm": "07:30"}})
        frames = _collect_until_done(ws)

    assert {"type": "assistant_text", "text": "Wecker fuer 7:30 Uhr gestellt.", "final": True} in frames


def test_device_tool_error_result_is_passed_to_llm(client, monkeypatch):
    """ok=false im tool_result wird dem LLM als Tool-Fehler gereicht."""
    transcript = []
    _scripted_llm(monkeypatch, [
        _tool_call("open_app", {"app": "spotify"}),
        {"role": "assistant", "content": "Das hat leider nicht geklappt."},
    ], transcript)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat",
                      "tools": [{"name": "open_app", "description": "Oeffnet eine App"}]})
        ws.send_json({"type": "text_input", "text": "Mach Spotify an"})
        tool_call = _next_frame(ws)
        ws.send_json({"type": "tool_result", "call_id": tool_call["call_id"],
                      "ok": False, "result": "App nicht installiert"})
        _collect_until_done(ws)

    tool_messages = [m for m in transcript[1]["messages"] if m["role"] == "tool"]
    assert "Tool-Fehler auf dem Geraet" in tool_messages[0]["content"]
    assert "App nicht installiert" in tool_messages[0]["content"]


def test_show_card_pushes_card_frame(client, monkeypatch):
    _scripted_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "generic", "title": "Info",
                                 "data": {"headline": "Hallo", "body": "Welt"}}),
        {"role": "assistant", "content": "Ich habe dir eine Karte geschickt."},
    ])

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Zeig mir eine Karte"})
        frames = _collect_until_done(ws)

    card_frames = [f for f in frames if f["type"] == "card"]
    assert card_frames == [{
        "type": "card",
        "card": {"type": "generic", "version": 1, "title": "Info",
                 "data": {"headline": "Hallo", "body": "Welt"}},
    }]
    # Karte kam VOR der finalen Antwort (parallel zur Sprachausgabe, 4.12)
    types = [f["type"] for f in frames]
    assert types.index("card") < types.index("assistant_text")


def test_tool_filler_with_specific_pattern_plays_before_tool(client, monkeypatch):
    """Der Admin-definierte Tool-Trigger (1.7d) feuert: Kalender-Tool ->
    Kalender-Filler aus dem Cache, vor der Antwort. Sprach-Turn = Audio-Input."""
    _scripted_llm(monkeypatch, [
        _tool_call("Calendar-list_events", {}),
        {"role": "assistant", "content": "Morgen hast du zwei Termine."},
    ])
    monkeypatch.setattr(mcp_gateway_module.mcp_gateway, "call_tool", AsyncMock(return_value="2 Termine"))
    monkeypatch.setattr(
        stream_module.stt_client, "transcribe", AsyncMock(return_value="Was steht morgen an?")
    )

    async def fake_xtts(text, voice_id, language=None):
        yield 24000, b"\x01\x02" * 600

    monkeypatch.setattr(stream_module.xtts_client, "stream", fake_xtts)
    piper_mock = AsyncMock(side_effect=RuntimeError("kein Piper noetig"))
    monkeypatch.setattr(stream_module.piper_client, "synthesize", piper_mock)

    # delay_ms=0: Tool-Filler darf sofort spielen (der Mock-Tool-Call ist
    # instant - mit Delay wuerde er per Design entfallen). Die thinking-
    # Filler behalten ihren Seed-Delay (1200ms) und feuern hier nie.
    trigger = repos.create_trigger("Kalender", "tool", "Calendar-*")
    filler = repos.create_filler("Kalender-Blick", "Ich schaue kurz in den Kalender.",
                                 trigger["id"], True, delay_ms=0)
    import wave
    path = filler_service.audio_path(filler["id"], settings.default_voice_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(24000)
        f.writeframes(b"\x09\x0a" * 600)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "audio_chunk", "data": pcm_to_b64(b"\x00\x00" * 1600)})
        ws.send_json({"type": "audio_end"})
        frames = _collect_until_done(ws)

    types = [f["type"] for f in frames]
    assert types.index("audio_chunk") < types.index("assistant_text")
    piper_mock.assert_not_called()


def test_tool_loop_terminates_at_iteration_limit(client, monkeypatch):
    """LLM will endlos Tools rufen -> ab der Obergrenze bekommt es keine
    Tools mehr angeboten und muss final antworten."""
    monkeypatch.setattr(settings, "tool_max_iterations", 2)

    calls = {"count": 0}

    async def stubborn_llm(messages, tools=None):
        calls["count"] += 1
        if tools:
            return _tool_call("Time-current_time", {}, call_id=f"call_{calls['count']}")
        return {"role": "assistant", "content": "Fertig ohne weitere Tools."}

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", stubborn_llm)
    monkeypatch.setattr(
        mcp_gateway_module.mcp_gateway, "list_openai_tools",
        AsyncMock(return_value=[{
            "type": "function",
            "function": {"name": "Time-current_time", "description": "",
                         "parameters": {"type": "object", "properties": {}}},
        }]),
    )
    monkeypatch.setattr(mcp_gateway_module.mcp_gateway, "call_tool", AsyncMock(return_value="12:00"))

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Und jetzt?"})
        frames = _collect_until_done(ws)

    assert {"type": "assistant_text", "text": "Fertig ohne weitere Tools.", "final": True} in frames
    # 2 Tool-Runden + 1 finale Runde ohne Tools
    assert calls["count"] == 3


def test_device_tool_timeout_becomes_tool_error(client, monkeypatch):
    monkeypatch.setattr(settings, "device_tool_timeout_s", 1)
    transcript = []
    _scripted_llm(monkeypatch, [
        _tool_call("open_app", {"app": "spotify"}),
        {"role": "assistant", "content": "Das hat leider nicht geklappt."},
    ], transcript)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({
            "type": "hello", "mode": "chat",
            "tools": [{"name": "open_app", "description": "Oeffnet eine App"}],
        })
        ws.send_json({"type": "text_input", "text": "Mach Spotify an"})
        tool_call = _next_frame(ws)
        assert tool_call["type"] == "tool_call"
        # Bewusst KEIN tool_result senden -> Timeout
        frames = _collect_until_done(ws)

    assert {"type": "assistant_text", "text": "Das hat leider nicht geklappt.", "final": True} in frames
    tool_messages = [m for m in transcript[1]["messages"] if m["role"] == "tool"]
    assert "Tool-Fehler" in tool_messages[0]["content"]


def test_tool_filler_skipped_when_tool_is_fast(client, monkeypatch):
    """Per-Filler-Delay beim Tool-Pfad: Ist das Tool vor Ablauf des Delays
    fertig, entfaellt der Filler - schnelle Tools werden nicht blockiert."""
    _scripted_llm(monkeypatch, [
        _tool_call("Calendar-list_events", {}),
        {"role": "assistant", "content": "Nichts los morgen."},
    ])
    monkeypatch.setattr(mcp_gateway_module.mcp_gateway, "call_tool", AsyncMock(return_value="leer"))
    monkeypatch.setattr(
        stream_module.stt_client, "transcribe", AsyncMock(return_value="Was steht morgen an?")
    )

    async def fake_xtts(text, voice_id, language=None):
        yield 24000, b"\x01\x02" * 600

    monkeypatch.setattr(stream_module.xtts_client, "stream", fake_xtts)
    piper_mock = AsyncMock(return_value=(b"\x05\x06" * 2205, 22050))
    monkeypatch.setattr(stream_module.piper_client, "synthesize", piper_mock)

    trigger = repos.create_trigger("Kalender", "tool", "Calendar-*")
    repos.create_filler("Kalender-Blick", "Ich schaue kurz in den Kalender.",
                        trigger["id"], True, delay_ms=500)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "audio_chunk", "data": pcm_to_b64(b"\x00\x00" * 1600)})
        ws.send_json({"type": "audio_end"})
        frames = _collect_until_done(ws)

    types = [f["type"] for f in frames]
    # Kein Filler vor der Antwort: erstes Audio kommt NACH assistant_text
    assert types.index("assistant_text") < types.index("audio_chunk")
    piper_mock.assert_not_called()


def test_tool_schemas_are_normalized_for_strict_validators(client, monkeypatch):
    """LM Studio (Zod) verlangt am Parameter-Root "type": "object" - Schemas
    aus MCP-Servern oder App-Manifesten ohne Root-type muessen normalisiert
    werden, sonst lehnt es den GESAMTEN Request mit 400
    invalid_union_discriminator ab (Ursache des Sprach-Turn-Fehlers)."""
    import asyncio as aio

    from orchestrator.schemas import DeviceTool
    from orchestrator.services.tool_executor import ToolExecutor

    # MCP-Tool ohne Root-type + mit $schema-Fremdschluessel
    monkeypatch.setattr(
        mcp_gateway_module.mcp_gateway, "list_openai_tools",
        AsyncMock(return_value=[{
            "type": "function",
            "function": {
                "name": "Time-current_time",
                "description": "Zeit",
                "parameters": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "properties": {"timezone": {"type": "string"}},
                },
            },
        }]),
    )

    async def card_push(envelope):
        pass

    executor = ToolExecutor(
        # Geraete-Tool mit kaputtem Schema (kein type, properties kein Dict)
        device_tools=[DeviceTool(name="set_alarm", parameters={"properties": None})],
        card_push=card_push,
    )
    tools = aio.get_event_loop_policy().new_event_loop().run_until_complete(
        executor.list_openai_tools()
    )

    for tool in tools:
        params = tool["function"]["parameters"]
        assert params["type"] == "object", tool["function"]["name"]
        assert isinstance(params["properties"], dict), tool["function"]["name"]
        assert "$schema" not in params

    # Inhalt bleibt erhalten (nur normalisiert, nicht plattgemacht)
    mcp_tool = next(t for t in tools if t["function"]["name"] == "Time-current_time")
    assert mcp_tool["function"]["parameters"]["properties"] == {"timezone": {"type": "string"}}


def test_show_card_with_ai_written_html(client, monkeypatch):
    """4.12 (v1.12): Findet das LLM kein passendes Layout, schreibt es selbst
    eine HTML-Karte - das html-Argument wird zur Envelope {type: "html",
    data.html}; Alt-Clients rendern dafuer die generic-Karte."""
    html = "<div style='color:teal'><b>3 Termine</b> morgen</div>"
    _scripted_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "termin_uebersicht", "title": "Termine",
                                 "data": {"headline": "3 Termine"}, "html": html}),
        {"role": "assistant", "content": "Hier ist deine Uebersicht."},
    ])

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Zeig meine Termine huebsch an"})
        frames = _collect_until_done(ws)

    card_frames = [f for f in frames if f["type"] == "card"]
    assert card_frames == [{
        "type": "card",
        "card": {"type": "html", "version": 1, "title": "Termine",
                 "data": {"headline": "3 Termine", "html": html}},
    }]


def test_show_card_description_offers_html_and_coding_agent(client):
    """Die Tool-Beschreibung fordert das LLM auf, bei fehlendem Layout selbst
    HTML zu schreiben - und verweist auf den Coding-Agenten, wenn es einen
    gibt."""
    import asyncio as aio

    from orchestrator.services.tool_executor import ToolExecutor

    async def card_push(envelope):
        pass

    loop = aio.get_event_loop_policy().new_event_loop()

    executor = ToolExecutor(card_push=card_push)
    tools = loop.run_until_complete(executor.list_openai_tools())
    show_card = next(t for t in tools if t["function"]["name"] == "show_card")
    assert "html" in show_card["function"]["parameters"]["properties"]
    assert "erstelle selbst ein Layout" in show_card["function"]["description"]
    assert "agent-coding" not in show_card["function"]["description"]

    repos.create_agent("coding", "Coding", "Schreibt Code und HTML.", "", "cloud/code")
    tools = loop.run_until_complete(executor.list_openai_tools())
    show_card = next(t for t in tools if t["function"]["name"] == "show_card")
    assert "agent-coding" in show_card["function"]["description"]
