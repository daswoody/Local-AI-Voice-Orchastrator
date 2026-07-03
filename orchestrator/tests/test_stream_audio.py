import asyncio
from unittest.mock import AsyncMock

from orchestrator import graph as graph_module
from orchestrator.audio import pcm_to_b64
from orchestrator.config import settings
from orchestrator.routers import stream as stream_module


def _collect_until_done(ws) -> list[dict]:
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == "done":
            return frames


async def _fake_xtts_stream(text, voice_id, language=None):
    yield 24000, b"\x01\x02" * 1200
    yield 24000, b"\x03\x04" * 1200


def _patch_pipeline(monkeypatch, llm_response="Antwort", llm_delay=0.0):
    async def fake_chat(messages):
        if llm_delay:
            await asyncio.sleep(llm_delay)
        return llm_response

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat", fake_chat)
    monkeypatch.setattr(
        stream_module.stt_client, "transcribe", AsyncMock(return_value="Wie ist das Wetter?")
    )
    monkeypatch.setattr(
        stream_module.piper_client,
        "synthesize",
        AsyncMock(return_value=(b"\x05\x06" * 2205, 22050)),
    )
    monkeypatch.setattr(stream_module.xtts_client, "stream", _fake_xtts_stream)


def test_audio_roundtrip(client, monkeypatch):
    """Kern von 1.11: Audio rein -> transcript, assistant_text, Audio raus."""
    _patch_pipeline(monkeypatch)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "audio_chunk", "audio": pcm_to_b64(b"\x00\x00" * 1600)})
        ws.send_json({"type": "audio_end"})
        frames = _collect_until_done(ws)

    types = [f["type"] for f in frames]
    assert types[0] == "transcript"
    assert frames[0] == {"type": "transcript", "text": "Wie ist das Wetter?", "final": True}
    assert {"type": "assistant_text", "text": "Antwort", "final": True} in frames
    audio_frames = [f for f in frames if f["type"] == "audio_chunk"]
    assert len(audio_frames) >= 1
    assert all(f["sample_rate"] == 24000 for f in audio_frames)
    # audio_end kommt nach dem letzten audio_chunk, done ist das letzte Frame
    assert types.index("audio_end") > types.index("audio_chunk")
    assert types[-1] == "done"


def test_text_input_in_chat_mode_stays_text_only(client, monkeypatch):
    _patch_pipeline(monkeypatch)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Hallo"})
        frames = _collect_until_done(ws)

    assert [f["type"] for f in frames] == ["assistant_text", "done"]


def test_text_input_in_talk_mode_gets_audio(client, monkeypatch):
    _patch_pipeline(monkeypatch)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "text_input", "text": "Hallo"})
        frames = _collect_until_done(ws)

    assert any(f["type"] == "audio_chunk" for f in frames)


def test_filler_plays_when_llm_is_slow(client, monkeypatch):
    _patch_pipeline(monkeypatch, llm_delay=0.5)
    monkeypatch.setattr(settings, "filler_delay_ms", 20)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "text_input", "text": "Erzaehl mir was Langes"})
        frames = _collect_until_done(ws)

    types = [f["type"] for f in frames]
    # Filler-Audio muss VOR dem assistant_text kommen - das ist der Sinn
    # der Filler-Strategie (Ueberbrueckung waehrend das LLM rechnet).
    assert types.index("audio_chunk") < types.index("assistant_text")


def test_no_filler_when_llm_is_fast(client, monkeypatch):
    _patch_pipeline(monkeypatch, llm_delay=0.0)
    monkeypatch.setattr(settings, "filler_delay_ms", 500)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "text_input", "text": "Kurze Frage"})
        frames = _collect_until_done(ws)

    types = [f["type"] for f in frames]
    # Erst Text, dann Audio: kein Filler vorweg
    assert types.index("assistant_text") < types.index("audio_chunk")


def test_interrupt_cancels_running_response(client, monkeypatch):
    _patch_pipeline(monkeypatch, llm_delay=5.0)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": "Langsame Frage"})
        ws.send_json({"type": "interrupt"})
        frame = ws.receive_json()

    # Barge-in: sofort done, kein assistant_text der abgebrochenen Antwort
    assert frame == {"type": "done"}


def test_stt_failure_reports_error_and_closes_turn(client, monkeypatch):
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(
        stream_module.stt_client, "transcribe", AsyncMock(side_effect=RuntimeError("kaputt"))
    )

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.send_json({"type": "audio_chunk", "audio": pcm_to_b64(b"\x00\x00" * 100)})
        ws.send_json({"type": "audio_end"})
        frames = _collect_until_done(ws)

    assert [f["type"] for f in frames] == ["error", "done"]


def test_audio_end_without_audio_is_error(client, monkeypatch):
    _patch_pipeline(monkeypatch)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.send_json({"type": "audio_end"})
        frame = ws.receive_json()

    assert frame["type"] == "error"
