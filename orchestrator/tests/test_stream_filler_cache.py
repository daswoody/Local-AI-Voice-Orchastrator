"""Laufzeit-Verhalten des Filler-Systems (1.7d) im WebSocket."""

import asyncio
import io
import wave
from unittest.mock import AsyncMock

from orchestrator import graph as graph_module, repos
from orchestrator.config import settings
from orchestrator.routers import stream as stream_module
from orchestrator.services import filler_service


def _write_filler_wav(path, rate=24000, marker=b"\x07\x08", samples=1200):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(marker * samples)


async def _fake_xtts_stream(text, voice_id, language=None):
    yield 24000, b"\x01\x02" * 1200


def test_cached_filler_is_used_instead_of_piper(client, monkeypatch):
    async def slow_chat(messages):
        await asyncio.sleep(0.3)
        return "Antwort"

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat", slow_chat)
    monkeypatch.setattr(stream_module.xtts_client, "stream", _fake_xtts_stream)
    monkeypatch.setattr(settings, "filler_delay_ms", 20)

    piper_mock = AsyncMock(side_effect=RuntimeError("Piper darf nicht gebraucht werden"))
    monkeypatch.setattr(stream_module.piper_client, "synthesize", piper_mock)

    # Vorgeneriertes Audio fuer ALLE thinking-Filler der Default-Stimme
    for filler in repos.fillers_for_kind("thinking"):
        _write_filler_wav(filler_service.audio_path(filler["id"], settings.default_voice_id))

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "text_input", "text": "Langsame Frage"})
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "done":
                break

    types = [f["type"] for f in frames]
    # Filler-Audio kam vor dem Text - und zwar aus dem Cache, nicht von Piper
    assert types.index("audio_chunk") < types.index("assistant_text")
    piper_mock.assert_not_called()


def test_piper_fallback_when_no_cached_audio(client, monkeypatch):
    async def slow_chat(messages):
        await asyncio.sleep(0.3)
        return "Antwort"

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat", slow_chat)
    monkeypatch.setattr(stream_module.xtts_client, "stream", _fake_xtts_stream)
    monkeypatch.setattr(settings, "filler_delay_ms", 20)

    piper_mock = AsyncMock(return_value=(b"\x05\x06" * 2205, 22050))
    monkeypatch.setattr(stream_module.piper_client, "synthesize", piper_mock)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "text_input", "text": "Langsame Frage"})
        while ws.receive_json()["type"] != "done":
            pass

    # Kein Cache vorhanden -> Piper hat den Filler-Text gesprochen,
    # und zwar einen aus der DB (Seed-Daten), nicht mehr die alte Konstante.
    piper_mock.assert_called_once()
    spoken = piper_mock.call_args.args[0]
    assert spoken in [f["text"] for f in repos.fillers_for_kind("thinking")]


def test_user_specific_voice_and_prompt_from_token(client, monkeypatch):
    captured = {}

    async def capture_chat(messages):
        captured["system"] = messages[0]["content"]
        return "Ok"

    async def capture_xtts(text, voice_id, language=None):
        captured["voice_id"] = voice_id
        yield 24000, b"\x01\x02" * 100

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat", capture_chat)
    monkeypatch.setattr(stream_module.xtts_client, "stream", capture_xtts)

    from orchestrator.security import hash_password
    repos.create_user("mia", hash_password("pw"), "Mia", 2,
                      "Du sprichst wie ein Pirat.", "default-de-male")
    login = client.post("/v1/auth/login", json={"username": "mia", "password": "pw", "device_name": "t"})
    token = login.json()["token"]

    with client.websocket_connect(f"/v1/assistant/stream?token={token}") as ws:
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "text_input", "text": "Hallo"})
        while ws.receive_json()["type"] != "done":
            pass

    assert captured["system"].startswith("Du sprichst wie ein Pirat.")
    assert captured["voice_id"] == "default-de-male"


def test_tts_failure_still_delivers_text(client, monkeypatch):
    """TTS-Fallback-Regel 4.13: kein Server-Audio -> App liest selbst vor.
    Der Turn muss trotzdem sauber mit assistant_text + done enden."""

    async def broken_xtts(text, voice_id, language=None):
        raise RuntimeError("Stimme ohne Sample")
        yield  # pragma: no cover

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat", AsyncMock(return_value="Antwort"))
    monkeypatch.setattr(stream_module.xtts_client, "stream", broken_xtts)
    monkeypatch.setattr(settings, "filler_enabled", False)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "text_input", "text": "Hallo"})
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "done":
                break

    types = [f["type"] for f in frames]
    assert "assistant_text" in types
    assert "audio_chunk" not in types
    assert "error" not in types
