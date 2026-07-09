"""Laufzeit-Verhalten des Filler-Systems (1.7d) im WebSocket."""

import asyncio
import wave
from unittest.mock import AsyncMock

from orchestrator import graph as graph_module, repos
from orchestrator.audio import pcm_to_b64
from orchestrator.config import settings
from orchestrator.routers import stream as stream_module
from orchestrator.services import filler_service


def _open(ws) -> None:
    assert ws.receive_json()["type"] == "session"


def _send_audio_input(ws) -> None:
    ws.send_json({"type": "audio_chunk", "data": pcm_to_b64(b"\x00\x00" * 1600)})
    ws.send_json({"type": "audio_end"})


def _drain(ws) -> list[dict]:
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == "done":
            return frames


def _write_filler_wav(path, rate=24000, marker=b"\x07\x08", samples=1200):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(marker * samples)


async def _fake_xtts_stream(text, voice_id, language=None):
    yield 24000, b"\x01\x02" * 1200


def _patch_base(monkeypatch, chat):
    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", chat)
    monkeypatch.setattr(
        stream_module.stt_client, "transcribe", AsyncMock(return_value="Langsame Frage")
    )
    monkeypatch.setattr(stream_module.xtts_client, "stream", _fake_xtts_stream)




def _set_thinking_delays(ms: int) -> None:
    """Delay aller Seed-Filler (thinking) fuer den Test setzen."""
    from orchestrator import repos
    for f in repos.fillers_for_kind("thinking"):
        repos.update_filler(f["id"], f["title"], f["text"], f["trigger_id"], True, delay_ms=ms)

async def _slow_chat(messages, tools=None):
    await asyncio.sleep(0.3)
    return {"role": "assistant", "content": "Antwort"}


def test_cached_filler_is_used_instead_of_piper(client, monkeypatch):
    _patch_base(monkeypatch, _slow_chat)
    _set_thinking_delays(20)

    piper_mock = AsyncMock(side_effect=RuntimeError("Piper darf nicht gebraucht werden"))
    monkeypatch.setattr(stream_module.piper_client, "synthesize", piper_mock)

    # Vorgeneriertes Audio fuer ALLE thinking-Filler der Default-Stimme
    for filler in repos.fillers_for_kind("thinking"):
        _write_filler_wav(filler_service.audio_path(filler["id"], settings.default_voice_id))

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        frames = _drain(ws)

    types = [f["type"] for f in frames]
    # Filler-Audio kam vor dem Text - und zwar aus dem Cache, nicht von Piper
    assert types.index("audio_chunk") < types.index("assistant_text")
    piper_mock.assert_not_called()


def test_piper_fallback_when_no_cached_audio(client, monkeypatch):
    _patch_base(monkeypatch, _slow_chat)
    _set_thinking_delays(20)

    piper_mock = AsyncMock(return_value=(b"\x05\x06" * 2205, 22050))
    monkeypatch.setattr(stream_module.piper_client, "synthesize", piper_mock)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        _drain(ws)

    # Kein Cache vorhanden -> Piper hat den Filler-Text gesprochen,
    # und zwar einen aus der DB (Seed-Daten).
    piper_mock.assert_called_once()
    spoken = piper_mock.call_args.args[0]
    assert spoken in [f["text"] for f in repos.fillers_for_kind("thinking")]


def test_user_specific_voice_and_prompt_from_token(client, monkeypatch):
    captured = {}

    async def capture_chat(messages, tools=None):
        captured["system"] = messages[0]["content"]
        return {"role": "assistant", "content": "Ok"}

    async def capture_xtts(text, voice_id, language=None):
        captured["voice_id"] = voice_id
        yield 24000, b"\x01\x02" * 100

    _patch_base(monkeypatch, capture_chat)
    monkeypatch.setattr(stream_module.xtts_client, "stream", capture_xtts)
    monkeypatch.setattr(settings, "filler_enabled", False)

    from orchestrator.security import hash_password
    repos.create_user("mia", hash_password("pw"), "Mia", 2,
                      "Du sprichst wie ein Pirat.", "default-de-male")
    login = client.post("/v1/auth/login", json={"username": "mia", "password": "pw", "device_name": "t"})
    token = login.json()["token"]

    # Token als Authorization-Header, wie die App es laut PROTOCOL.md tut
    with client.websocket_connect(
        "/v1/assistant/stream", headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        _drain(ws)

    assert captured["system"].startswith("Du sprichst wie ein Pirat.")
    assert captured["voice_id"] == "default-de-male"


def test_tts_failure_still_delivers_text(client, monkeypatch):
    """TTS-Fallback-Regel 4.13: kein Server-Audio -> App liest selbst vor.
    Der Turn muss trotzdem sauber mit assistant_text + done enden."""

    async def broken_xtts(text, voice_id, language=None):
        raise RuntimeError("Stimme ohne Sample")
        yield  # pragma: no cover

    _patch_base(monkeypatch, AsyncMock(return_value={"role": "assistant", "content": "Antwort"}))
    monkeypatch.setattr(stream_module.xtts_client, "stream", broken_xtts)
    monkeypatch.setattr(settings, "filler_enabled", False)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        frames = _drain(ws)

    types = [f["type"] for f in frames]
    assert "assistant_text" in types
    assert "audio_chunk" not in types
    assert "error" not in types


def test_llm_error_detail_reaches_the_client(client, monkeypatch):
    """Diagnose ohne VM-Zugriff: Der echte Fehlergrund (z. B. LiteLLM/
    LM-Studio-Fehlerbody) steht im error-Frame, nicht nur im Server-Log."""
    _patch_base(monkeypatch, AsyncMock(
        side_effect=RuntimeError("LLM-Fehler 500 (Modell test): chat template error")
    ))

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        frames = _drain(ws)

    error = next(f for f in frames if f["type"] == "error")
    assert "LLM-Fehler 500" in error["message"]
    assert "chat template error" in error["message"]


def test_filler_path_failure_never_kills_the_turn(client, monkeypatch):
    """Filler ist Komfort: selbst ein crashender Filler-Pfad (z. B. kaputte
    DB-Zeile, defekte Cache-Datei) darf den Sprach-Turn nicht abbrechen."""
    _patch_base(monkeypatch, _slow_chat)
    monkeypatch.setattr(
        stream_module.filler_service, "select_filler",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaputte Filler-Zeile")),
    )

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        frames = _drain(ws)

    types = [f["type"] for f in frames]
    assert "assistant_text" in types
    assert "error" not in types
    assert types[-1] == "done"
