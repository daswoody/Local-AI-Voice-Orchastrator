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
    der zentralen Historie (Phase 2.5) und tool_activity (v1.12.1)."""
    frame = ws.receive_json()
    while frame["type"] in ("conversation", "tool_activity"):
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


def test_show_card_description_adapts_to_card_agent(client):
    """Ohne code-card-Agent soll das LLM selbst HTML liefern; mit Agent sagt
    die Beschreibung: nur Daten liefern, das Layout schreibt code-card."""
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
    assert "code-card" not in show_card["function"]["description"]

    repos.create_agent("code-card", "Karten-Layouter", "Schreibt Karten-HTML.",
                       "", "cloud/code")
    tools = loop.run_until_complete(executor.list_openai_tools())
    show_card = next(t for t in tools if t["function"]["name"] == "show_card")
    assert "code-card" in show_card["function"]["description"]
    assert "KEIN html-Feld" in show_card["function"]["description"]


# ---- Tool-Aktivitaet im Verlauf (v1.12.1) ----------------------------------------


def test_tool_activity_frames_running_and_done(client, monkeypatch):
    """Der Client sieht live, was die KI tut: tool_activity running vor dem
    Tool, done danach - und beides VOR der finalen Antwort."""
    _scripted_llm(monkeypatch, [
        _tool_call("Time-current_time", {}),
        {"role": "assistant", "content": "Es ist 12:00 Uhr."},
    ])
    monkeypatch.setattr(mcp_gateway_module.mcp_gateway, "call_tool",
                        AsyncMock(return_value="12:00"))

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Wie spaet?"})
        frames = _collect_until_done(ws)

    activity = [f for f in frames if f["type"] == "tool_activity"]
    assert activity == [
        {"type": "tool_activity", "tool": "Time-current_time", "status": "running"},
        {"type": "tool_activity", "tool": "Time-current_time", "status": "done"},
    ]
    types = [f["type"] for f in frames]
    assert types.index("tool_activity") < types.index("assistant_text")


def test_tool_activity_reports_error_status(client, monkeypatch):
    _scripted_llm(monkeypatch, [
        _tool_call("Time-current_time", {}),
        {"role": "assistant", "content": "Das Tool klemmt gerade."},
    ])
    monkeypatch.setattr(mcp_gateway_module.mcp_gateway, "call_tool",
                        AsyncMock(side_effect=RuntimeError("Gateway down")))

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Wie spaet?"})
        frames = _collect_until_done(ws)

    statuses = [f["status"] for f in frames if f["type"] == "tool_activity"]
    assert statuses == ["running", "error"]


def test_show_card_emits_no_tool_activity(client, monkeypatch):
    """show_card ist rein visuell - die Karte selbst IST die Anzeige."""
    _scripted_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "generic", "data": {"headline": "Hi"}}),
        {"role": "assistant", "content": "Karte ist da."},
    ])

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Karte bitte"})
        frames = _collect_until_done(ws)

    assert [f for f in frames if f["type"] == "tool_activity"] == []


def test_tool_activity_is_persisted_with_assistant_message(client, monkeypatch):
    """Auch die Historie zeigt, was die KI getan hat: die Tool-Liste haengt
    an der Assistant-Message (Endzustand, nicht die running-Zwischenschritte)."""
    from orchestrator.config import settings as app_settings

    _scripted_llm(monkeypatch, [
        _tool_call("Time-current_time", {}),
        {"role": "assistant", "content": "12:00 Uhr."},
    ])
    monkeypatch.setattr(mcp_gateway_module.mcp_gateway, "call_tool",
                        AsyncMock(return_value="12:00"))

    login = client.post("/v1/auth/login", json={
        "username": app_settings.admin_username,
        "password": app_settings.admin_password, "device_name": "t"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    with client.websocket_connect("/v1/assistant/stream", headers=headers) as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Wie spaet?"})
        frames = _collect_until_done(ws)

    conversation_id = next(f for f in frames if f["type"] == "conversation")["conversation_id"]
    detail = client.get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    assistant = next(m for m in detail["messages"] if m["role"] == "assistant")
    assert assistant["tools"] == [{"tool": "Time-current_time", "status": "done"}]


# ---- show_card-Robustheit (v1.12.1) -----------------------------------------------


def test_show_card_accepts_html_inside_data(client, monkeypatch):
    """Modelle legen das HTML gern in data.html statt ins html-Feld - beides
    muss zur html-Envelope werden."""
    _scripted_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "uhrzeit", "title": "Uhrzeit",
                                 "data": {"html": "<b>19:03</b>"}}),
        {"role": "assistant", "content": "Bitte sehr."},
    ])

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Uhrzeit als Karte"})
        frames = _collect_until_done(ws)

    card = next(f for f in frames if f["type"] == "card")["card"]
    assert card["type"] == "html"
    assert card["data"]["html"] == "<b>19:03</b>"


def test_show_card_accepts_data_as_string(client, monkeypatch):
    """data als String (HTML oder JSON-String) statt Objekt - tolerant parsen."""
    _scripted_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "html", "data": "<p>Hallo</p>"}),
        {"role": "assistant", "content": "Karte ist da."},
    ])

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Karte"})
        frames = _collect_until_done(ws)

    card = next(f for f in frames if f["type"] == "card")["card"]
    assert card["type"] == "html"
    assert card["data"]["html"] == "<p>Hallo</p>"


def test_show_card_html_without_html_gives_llm_feedback(client, monkeypatch):
    """card_type 'html' ohne HTML-Inhalt: keine leere Karte pushen, sondern
    dem LLM ein korrigierbares Fehler-Ergebnis geben."""
    transcript = []
    _scripted_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "html", "title": "Uhrzeit in Tokio",
                                 "data": {}}),
        {"role": "assistant", "content": "Da ist etwas schiefgelaufen."},
    ], transcript)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Uhrzeit als Karte"})
        frames = _collect_until_done(ws)

    assert [f for f in frames if f["type"] == "card"] == []
    tool_messages = [m for m in transcript[1]["messages"] if m["role"] == "tool"]
    assert "html-Feld" in tool_messages[0]["content"]


# ---- Karten-Agent code-card (v1.12.2) ----------------------------------------------


def _card_agent_llm(monkeypatch, main_script: list[dict], agent_reply: str,
                    agent_calls: list | None = None):
    """Fake-LLM: Haupt-Modell (model=None) spielt das Skript, der
    code-card-Agent (eigenes Modell) antwortet mit agent_reply."""
    responses = list(main_script)

    async def fake_chat(messages, tools=None, model=None):
        if model == "cloud/card":
            if agent_calls is not None:
                agent_calls.append({"messages": [dict(m) for m in messages]})
            return {"role": "assistant", "content": agent_reply}
        return responses.pop(0)

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", fake_chat)


def test_card_agent_writes_html_when_llm_sends_only_data(client, monkeypatch):
    """Der Praxisfall des Nutzers: kleines lokales Modell liefert nur Titel +
    Daten (kein html-Feld) -> der code-card-Agent schreibt das Layout, die
    Karte kommt fertig gerendert an (kein leerer Kasten mehr)."""
    repos.create_agent("code-card", "Karten-Layouter", "Schreibt Karten-HTML.",
                       "", "cloud/card")
    agent_calls = []
    _card_agent_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "html", "title": "Uhrzeit Tokio",
                                 "data": {"zeit": "19:03", "zone": "JST"}}),
        {"role": "assistant", "content": "Hier ist deine Karte!"},
    ], agent_reply="```html\n<div><b>19:03</b> JST</div>\n```", agent_calls=agent_calls)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Uhrzeit in Tokio als Karte"})
        frames = _collect_until_done(ws)

    card = next(f for f in frames if f["type"] == "card")["card"]
    assert card["type"] == "html"
    # Markdown-Zaeune sind entfernt, die Daten stecken im HTML
    assert card["data"]["html"] == "<div><b>19:03</b> JST</div>"
    assert card["title"] == "Uhrzeit Tokio"

    # Der Agent hat Titel + Daten im Auftrag gesehen
    task = agent_calls[0]["messages"][1]["content"]
    assert "Uhrzeit Tokio" in task
    assert "19:03" in task

    # Der Nutzer sieht die Delegation als Aktivitaets-Chip
    activity = [f for f in frames if f["type"] == "tool_activity"]
    assert {"type": "tool_activity", "tool": "agent-code-card", "status": "running"} in activity
    assert {"type": "tool_activity", "tool": "agent-code-card", "status": "done"} in activity


def test_card_agent_kicks_in_for_unknown_type_without_generic_data(client, monkeypatch):
    """Unbekannter Kartentyp ohne headline/body wuerde als leerer Kasten
    enden -> auch dann schreibt code-card das Layout."""
    repos.create_agent("code-card", "Karten-Layouter", "Schreibt Karten-HTML.",
                       "", "cloud/card")
    _card_agent_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "uhrzeit_tokio",
                                 "data": {"zeit": "19:03"}}),
        {"role": "assistant", "content": "Bitte sehr."},
    ], agent_reply="<p>19:03</p>")

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Uhrzeit als Karte"})
        frames = _collect_until_done(ws)

    card = next(f for f in frames if f["type"] == "card")["card"]
    assert card["type"] == "html"
    assert card["data"]["html"] == "<p>19:03</p>"


def test_unknown_type_with_generic_data_needs_no_agent(client, monkeypatch):
    """Unbekannter Typ MIT headline/body rendert ueber das generic-Fallback -
    keine Delegation, Karte geht unveraendert raus."""
    repos.create_agent("code-card", "Karten-Layouter", "Schreibt Karten-HTML.",
                       "", "cloud/card")
    _card_agent_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "hinweis",
                                 "data": {"headline": "Info", "body": "Alles ok"}}),
        {"role": "assistant", "content": "Bitte sehr."},
    ], agent_reply="DARF NICHT GENUTZT WERDEN")

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Karte"})
        frames = _collect_until_done(ws)

    card = next(f for f in frames if f["type"] == "card")["card"]
    assert card["type"] == "hinweis"
    assert "html" not in card["data"]


def test_card_agent_failure_is_correctable_tool_error(client, monkeypatch):
    """Liefert der Karten-Agent kein HTML (oder faellt aus), bekommt das
    Haupt-LLM einen Tool-Fehler statt einer leeren Karte."""
    repos.create_agent("code-card", "Karten-Layouter", "Schreibt Karten-HTML.",
                       "", "cloud/card")
    transcript = []

    async def fake_chat(messages, tools=None, model=None):
        if model == "cloud/card":
            return {"role": "assistant", "content": "Ich kann gerade nicht."}
        transcript.append([dict(m) for m in messages])
        if len(transcript) == 1:
            return _tool_call("show_card", {"card_type": "html", "data": {"x": 1}})
        return {"role": "assistant", "content": "Karte klappt gerade nicht."}

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", fake_chat)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Karte"})
        frames = _collect_until_done(ws)

    assert [f for f in frames if f["type"] == "card"] == []
    tool_messages = [m for m in transcript[-1] if m["role"] == "tool"]
    assert "Tool-Fehler bei agent-code-card" in tool_messages[0]["content"]
    statuses = [f["status"] for f in frames if f["type"] == "tool_activity"
                and f["tool"] == "agent-code-card"]
    assert statuses == ["running", "error"]


# ---- Leere-Karten-Nachschlag + Karten-Spam (v1.12.3) --------------------------------


def test_card_agent_kicks_in_for_generic_without_headline(client, monkeypatch):
    """Praxisfall aus dem Chat-Screenshot: Modell waehlt card_type generic,
    legt aber eigene Keys statt headline/body in data -> vorher leerer
    Kasten, jetzt schreibt code-card das Layout."""
    repos.create_agent("code-card", "Karten-Layouter", "Schreibt Karten-HTML.",
                       "", "cloud/card")
    _card_agent_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "generic", "title": "Aktuelle Zeit in Tokio",
                                 "data": {"zeit": "20:15", "zone": "Asia/Tokyo"}}),
        {"role": "assistant", "content": "Bitte sehr."},
    ], agent_reply="<p>20:15 (Asia/Tokyo)</p>")

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Uhrzeit als Karte"})
        frames = _collect_until_done(ws)

    card = next(f for f in frames if f["type"] == "card")["card"]
    assert card["type"] == "html"
    assert card["data"]["html"] == "<p>20:15 (Asia/Tokyo)</p>"


def test_without_card_agent_data_is_synthesized_into_generic(client, monkeypatch):
    """Ohne code-card-Agent darf trotzdem keine leere Karte ankommen: die
    data-Keys werden als headline/body aufbereitet."""
    _scripted_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "uhrzeit", "title": "Uhrzeit Tokio",
                                 "data": {"zeit": "20:15", "zone": "Asia/Tokyo"}}),
        {"role": "assistant", "content": "Bitte sehr."},
    ])

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Uhrzeit als Karte"})
        frames = _collect_until_done(ws)

    card = next(f for f in frames if f["type"] == "card")["card"]
    assert card["type"] == "generic"
    assert card["data"]["headline"] == "Uhrzeit Tokio"
    assert "zeit: 20:15" in card["data"]["body"]
    assert "zone: Asia/Tokyo" in card["data"]["body"]


def test_identical_card_is_pushed_only_once_per_turn(client, monkeypatch):
    """Praxisfall: Modelle rufen show_card gern 2-3x mit denselben Daten auf
    -> nur die erste Karte geht raus, die Wiederholung bekommt einen Hinweis."""
    arguments = {"card_type": "generic", "title": "Info",
                 "data": {"headline": "Hallo", "body": "Welt"}}
    transcript = []
    _scripted_llm(monkeypatch, [
        _tool_call("show_card", arguments, call_id="call_1"),
        _tool_call("show_card", arguments, call_id="call_2"),
        {"role": "assistant", "content": "Karte ist da."},
    ], transcript)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Karte bitte"})
        frames = _collect_until_done(ws)

    assert len([f for f in frames if f["type"] == "card"]) == 1
    tool_messages = [m for m in transcript[2]["messages"] if m["role"] == "tool"]
    assert "bereits angezeigt" in tool_messages[-1]["content"]


# ---- code-card-Aufruf-Disziplin (v1.12.4) -------------------------------------------


def test_duplicate_request_triggers_no_second_generation(client, monkeypatch):
    """Praxisfall: 4x 'Agent: code-card' fuer EINE Karte. Die identische
    show_card-Anfrage wird jetzt VOR der Generierung abgefangen - der Agent
    laeuft genau einmal."""
    repos.create_agent("code-card", "Karten-Layouter", "Schreibt Karten-HTML.",
                       "", "cloud/card")
    agent_calls = []
    arguments = {"card_type": "html", "title": "Uhrzeit", "data": {"zeit": "20:43"}}
    _card_agent_llm(monkeypatch, [
        _tool_call("show_card", arguments, call_id="call_1"),
        _tool_call("show_card", arguments, call_id="call_2"),
        _tool_call("show_card", arguments, call_id="call_3"),
        {"role": "assistant", "content": "Fertig."},
    ], agent_reply="<p>20:43</p>", agent_calls=agent_calls)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Uhrzeit als Karte"})
        frames = _collect_until_done(ws)

    assert len(agent_calls) == 1
    assert len([f for f in frames if f["type"] == "card"]) == 1
    agent_chips = [f for f in frames if f["type"] == "tool_activity"
                   and f["tool"] == "agent-code-card"]
    assert len(agent_chips) == 2  # genau EIN Lauf: running + done


def test_generation_limit_falls_back_to_synthesized_generic(client, monkeypatch):
    """Mehr als zwei VERSCHIEDENE Karten pro Turn: ab der dritten wird nicht
    mehr generiert, sondern aus den Daten eine generic-Karte gebaut."""
    repos.create_agent("code-card", "Karten-Layouter", "Schreibt Karten-HTML.",
                       "", "cloud/card")
    agent_calls = []
    _card_agent_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "html", "data": {"a": 1}}, "c1"),
        _tool_call("show_card", {"card_type": "html", "data": {"b": 2}}, "c2"),
        _tool_call("show_card", {"card_type": "html", "data": {"c": 3}}, "c3"),
        {"role": "assistant", "content": "Fertig."},
    ], agent_reply="<p>x</p>", agent_calls=agent_calls)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Karten"})
        frames = _collect_until_done(ws)

    assert len(agent_calls) == 2
    cards = [f["card"] for f in frames if f["type"] == "card"]
    assert len(cards) == 3
    assert [c["type"] for c in cards] == ["html", "html", "generic"]
    assert "c: 3" in cards[2]["data"]["body"]


def test_show_card_success_result_warns_against_repeating_content(client, monkeypatch):
    """Das Erfolgs-Ergebnis sagt dem Modell explizit, den Karteninhalt nicht
    nochmal als Text/Tabelle auszugeben."""
    transcript = []
    _scripted_llm(monkeypatch, [
        _tool_call("show_card", {"card_type": "generic",
                                 "data": {"headline": "Hi", "body": "Welt"}}),
        {"role": "assistant", "content": "Karte ist da."},
    ], transcript)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Karte"})
        _collect_until_done(ws)

    tool_messages = [m for m in transcript[1]["messages"] if m["role"] == "tool"]
    assert "Wiederhole" in tool_messages[0]["content"]
