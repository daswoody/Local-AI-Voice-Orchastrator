"""Zentrale Chat-Historie (Phase 2.5): REST-Endpoints + Persistenz aus dem
WebSocket-Stream + image_input-Erweiterung."""

from unittest.mock import AsyncMock

from orchestrator import graph as graph_module
from orchestrator import repos
from orchestrator.config import settings
from orchestrator.graph import _extract_image_data_url


def _mock_llm(monkeypatch, content="Hallo zurueck!"):
    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    mock = AsyncMock(return_value={"role": "assistant", "content": content})
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", mock)
    return mock


def _admin_ws_headers(client) -> dict:
    response = client.post(
        "/v1/auth/login",
        json={"username": settings.admin_username, "password": settings.admin_password,
              "device_name": "pytest"},
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_conversations_require_token(client):
    assert client.get("/v1/conversations").status_code == 401


def test_ws_turn_persists_conversation(client, monkeypatch):
    _mock_llm(monkeypatch)
    headers = _admin_ws_headers(client)

    with client.websocket_connect("/v1/assistant/stream", headers=headers) as ws:
        ws.receive_json()  # session
        ws.send_json({"type": "hello", "mode": "chat", "device": {"name": "Test-PC"}})
        ws.send_json({"type": "text_input", "text": "Hallo Assistenz"})

        conversation = ws.receive_json()
        assert conversation["type"] == "conversation"
        conversation_id = conversation["conversation_id"]
        assert ws.receive_json()["type"] == "assistant_text"
        assert ws.receive_json() == {"type": "done"}

    listing = client.get("/v1/conversations", headers=headers).json()["conversations"]
    assert len(listing) == 1
    assert listing[0]["id"] == conversation_id
    assert listing[0]["title"] == "Hallo Assistenz"
    assert listing[0]["device_name"] == "Test-PC"
    assert listing[0]["message_count"] == 2

    detail = client.get(f"/v1/conversations/{conversation_id}", headers=headers).json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assert detail["messages"][1]["content"] == "Hallo zurueck!"


def test_ws_hello_continues_existing_conversation(client, monkeypatch):
    _mock_llm(monkeypatch)
    headers = _admin_ws_headers(client)
    conversation = repos.create_conversation(settings.admin_username, "Test-PC", "Alt")
    repos.append_message(conversation["id"], "user", "erste Frage")

    with client.websocket_connect("/v1/assistant/stream", headers=headers) as ws:
        ws.receive_json()  # session
        ws.send_json({"type": "hello", "mode": "chat", "conversation_id": conversation["id"]})
        confirmed = ws.receive_json()
        assert confirmed == {"type": "conversation", "conversation_id": conversation["id"]}

        ws.send_json({"type": "text_input", "text": "zweite Frage"})
        assert ws.receive_json()["type"] == "assistant_text"
        assert ws.receive_json() == {"type": "done"}

    messages = repos.list_messages(conversation["id"])
    assert [m["content"] for m in messages] == ["erste Frage", "zweite Frage", "Hallo zurueck!"]


def test_foreign_conversation_is_hidden_and_undeletable(client):
    headers = _admin_ws_headers(client)
    foreign = repos.create_conversation("andere-userin", "Handy", "Privates")

    assert client.get(f"/v1/conversations/{foreign['id']}", headers=headers).status_code == 404
    assert client.delete(f"/v1/conversations/{foreign['id']}", headers=headers).status_code == 404
    # Gast-Gespraeche (username NULL) tauchen in keiner Liste auf.
    repos.create_conversation(None, "Satellit", "Gastfrage")
    assert client.get("/v1/conversations", headers=headers).json()["conversations"] == []


def test_delete_own_conversation(client):
    headers = _admin_ws_headers(client)
    conversation = repos.create_conversation(settings.admin_username, "PC", "Wegwerfen")
    response = client.delete(f"/v1/conversations/{conversation['id']}", headers=headers)
    assert response.status_code == 200
    assert repos.get_conversation(conversation["id"]) is None


def test_image_input_reaches_llm_multimodal(client, monkeypatch):
    mock = _mock_llm(monkeypatch, content="Auf dem Bild ist ein Fenster.")
    headers = _admin_ws_headers(client)

    with client.websocket_connect("/v1/assistant/stream", headers=headers) as ws:
        ws.receive_json()  # session
        ws.send_json({"type": "hello", "mode": "assist"})
        ws.send_json({"type": "image_input", "data": "aGFsbG8=", "mime": "image/png",
                      "text": "Was siehst du?"})

        conversation = ws.receive_json()
        assert conversation["type"] == "conversation"
        assert ws.receive_json()["type"] == "assistant_text"
        assert ws.receive_json() == {"type": "done"}

    # Die user-Message ging multimodal (Text + data-URL) an LiteLLM ...
    messages = mock.call_args.args[0]
    user_content = [m for m in messages if m["role"] == "user"][-1]["content"]
    assert user_content[0] == {"type": "text", "text": "Was siehst du?"}
    assert user_content[1]["image_url"]["url"] == "data:image/png;base64,aGFsbG8="

    # ... und in der Historie ist der Turn als Bild markiert (ohne Rohdaten).
    detail = client.get(
        f"/v1/conversations/{conversation['conversation_id']}", headers=headers
    ).json()
    assert detail["messages"][0]["has_image"] is True
    assert "aGFsbG8" not in detail["messages"][0]["content"]


def test_image_input_size_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "max_image_b64_bytes", 10)
    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.receive_json()  # session
        ws.send_json({"type": "image_input", "data": "A" * 11})
        assert ws.receive_json() == {"type": "error", "message": "Bild zu gross"}


def test_extract_image_data_url_from_tool_result():
    result = '{"image_b64": "QUJD", "mime": "image/jpeg"}'
    assert _extract_image_data_url(result) == "data:image/jpeg;base64,QUJD"
    assert _extract_image_data_url("kein bild") is None
    assert _extract_image_data_url('{"image_b64": ""}') is None


# ---- Voice-First + Gespraechsgedaechtnis (v1.13) ------------------------------------


def test_voice_turn_gets_voice_mode_prompt_text_turn_does_not(client, monkeypatch):
    """Voice-First: gesprochene Frage -> Sprachmodus-Anweisung im
    System-Prompt (kurz antworten, Umfang in Karten); getippte Frage nicht."""
    from unittest.mock import AsyncMock as AM

    from orchestrator.audio import pcm_to_b64
    from orchestrator.routers import stream as stream_module

    captured = []

    async def capture_chat(messages, tools=None, model=None):
        captured.append(messages[0]["content"])
        return {"role": "assistant", "content": "Kurz und knapp."}

    monkeypatch.setattr(graph_module.weaviate_client, "search", AM(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", capture_chat)
    monkeypatch.setattr(stream_module.stt_client, "transcribe", AM(return_value="Frage"))

    async def fake_xtts(text, voice_id, language=None):
        yield 24000, b"\x01\x02" * 100

    monkeypatch.setattr(stream_module.xtts_client, "stream", fake_xtts)
    monkeypatch.setattr(settings, "filler_enabled", False)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.receive_json()  # session
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Getippte Frage"})
        while ws.receive_json()["type"] != "done":
            pass
        ws.send_json({"type": "audio_chunk", "data": pcm_to_b64(b"\x00\x00" * 1600)})
        ws.send_json({"type": "audio_end"})
        while ws.receive_json()["type"] != "done":
            pass

    assert "Sprachmodus" not in captured[0]
    assert "Sprachmodus" in captured[1]
    assert "show_card" in captured[1]


def test_history_with_card_data_reaches_the_llm(client, monkeypatch):
    """Use-Case 'Karte gepusht -> naechste Frage kennt die Karten-Infos':
    vorherige Turns inkl. Karten-DATEN (ohne Layout-HTML) stehen im
    LLM-Kontext des Folge-Turns, die aktuelle Frage genau einmal am Ende."""
    from unittest.mock import AsyncMock as AM

    headers = _admin_ws_headers(client)
    conversation = repos.create_conversation(settings.admin_username, "Test-PC", "Fluege")
    repos.append_message(conversation["id"], "user", "Zeig mir Fluege nach Schweden")
    repos.append_message(
        conversation["id"], "assistant", "Hier ist deine Flugsuche-Karte.",
        cards=[{"type": "html", "title": "Flugsuche",
                "data": {"ziel": "Stockholm", "html": "<form>...</form>"}}],
    )

    captured = {}

    async def capture_chat(messages, tools=None, model=None):
        captured["messages"] = [dict(m) for m in messages]
        return {"role": "assistant", "content": "Klar, Goeteborg geht auch."}

    monkeypatch.setattr(graph_module.weaviate_client, "search", AM(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", capture_chat)

    with client.websocket_connect("/v1/assistant/stream", headers=headers) as ws:
        ws.receive_json()  # session
        ws.send_json({"type": "hello", "mode": "chat", "conversation_id": conversation["id"]})
        assert ws.receive_json()["type"] == "conversation"
        ws.send_json({"type": "text_input", "text": "Aendere das Ziel auf Goeteborg"})
        while ws.receive_json()["type"] != "done":
            pass

    messages = captured["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "Zeig mir Fluege nach Schweden"}
    assert messages[2]["role"] == "assistant"
    # Karten-Daten sind drin, das Layout-HTML nicht
    assert "Stockholm" in messages[2]["content"]
    assert "Flugsuche" in messages[2]["content"]
    assert "<form>" not in messages[2]["content"]
    # Aktuelle Frage genau einmal, als letzte Message
    assert messages[-1] == {"role": "user", "content": "Aendere das Ziel auf Goeteborg"}
    assert sum(1 for m in messages if m["content"] == "Aendere das Ziel auf Goeteborg") == 1
