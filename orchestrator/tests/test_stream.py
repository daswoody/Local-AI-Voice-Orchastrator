from unittest.mock import AsyncMock

from orchestrator import graph as graph_module


def test_text_input_roundtrip_over_websocket(client, monkeypatch):
    monkeypatch.setattr(
        graph_module.weaviate_client, "search", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        graph_module.litellm_client, "chat_message",
        AsyncMock(return_value={"role": "assistant", "content": "Hallo zurueck!"}),
    )

    with client.websocket_connect("/v1/assistant/stream") as ws:
        session = ws.receive_json()
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Hallo"})

        assistant_text = ws.receive_json()
        done = ws.receive_json()

    assert session["type"] == "session" and session["session_id"]
    assert assistant_text == {"type": "assistant_text", "text": "Hallo zurueck!", "final": True}
    assert done == {"type": "done"}
