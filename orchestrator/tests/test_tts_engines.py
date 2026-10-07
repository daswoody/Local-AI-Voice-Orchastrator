"""TTS-Engine-Wechsel der Hauptstimme (v1.17): Wahl im Panel, Rueckfallebene,
Probehoeren, Filler mit anderen Engines, Sample-Transkript."""

import base64
import io
import math
import socket
import threading
import wave
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from orchestrator import graph as graph_module, repos
from orchestrator.audio import pcm_to_b64
from orchestrator.config import settings
from orchestrator.routers import stream as stream_module
from orchestrator.services import filler_service, tts_engines
from orchestrator.services.tts_client import (
    ContractClient,
    normalize_base_url,
    piper_client,
    xtts_client,
)


def _wav_bytes(rate=16000, seconds=0.2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x01" * int(rate * seconds))
    return buffer.getvalue()


def _write_sample(voice_id: str) -> Path:
    path = Path(settings.voices_dir) / f"{voice_id}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav_bytes())
    return path


def _fake_engine(marker: bytes, calls: list | None = None):
    async def stream(text, voice_id=None, language=None):
        if calls is not None:
            calls.append((text, voice_id))
        yield 24000, marker * 1200

    return stream


def _demo_engine(monkeypatch, stream=None, url: str = "http://tts-demo:8000") -> str:
    """Eine dritte Engine neben XTTS und Piper: eine eingetragene
    Vertrags-Engine, deren Client (optional) durch `stream` ersetzt wird."""
    repos.upsert_contract_engine("demo", url, "Demo-TTS")
    if stream is not None:
        async def fake(self, text, voice_id, language=None, load_timeout_s=0.0):
            async for item in stream(text, voice_id, language):
                yield item

        monkeypatch.setattr(ContractClient, "stream", fake)
    return "demo"


def _voice_turn(client, monkeypatch) -> list[dict]:
    """Ein kompletter Sprach-Turn (Audio rein -> Sprachantwort raus)."""
    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        graph_module.litellm_client, "chat_message",
        AsyncMock(return_value={"role": "assistant", "content": "Antwort"}),
    )
    monkeypatch.setattr(stream_module.stt_client, "transcribe", AsyncMock(return_value="Frage"))
    monkeypatch.setattr(settings, "filler_enabled", False)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "audio_chunk", "data": pcm_to_b64(b"\x00\x00" * 1600)})
        ws.send_json({"type": "audio_end"})
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == "done":
                return frames


def _spoken(frames: list[dict]) -> bytes:
    return b"".join(base64.b64decode(f["data"]) for f in frames if f["type"] == "audio_chunk")


def _ok_status(monkeypatch, status="ok"):
    async def fake_status(engine_id):
        return {"status": status, "detail": "" if status == "ok" else "Connection refused"}

    monkeypatch.setattr(tts_engines, "engine_status", fake_status)


# ---- Aktive Engine ---------------------------------------------------------------


def test_active_engine_defaults_to_xtts_and_follows_env_and_admin(monkeypatch):
    assert tts_engines.active_engine() == "xtts"

    monkeypatch.setattr(settings, "tts_engine", "piper")
    assert tts_engines.active_engine() == "piper"

    # Admin-Wahl schlaegt .env
    tts_engines.set_active_engine("xtts")
    assert tts_engines.active_engine() == "xtts"


def test_unknown_stored_engine_falls_back_to_xtts():
    """Z. B. eine Engine, die es in einer spaeteren Version nicht mehr gibt."""
    repos.set_setting(tts_engines.ACTIVE_ENGINE_SETTING, "gibtsnicht")
    assert tts_engines.active_engine() == "xtts"


def test_admin_overview_and_activation(client, admin_headers, monkeypatch):
    _ok_status(monkeypatch)

    body = client.get("/v1/admin/tts", headers=admin_headers).json()
    assert body["active_engine"] == "xtts"
    by_id = {engine["id"]: engine for engine in body["engines"]}
    # Breeze und Qwen3 laufen seit v1.22 ueber audio.cpp, nichts vorbelegt
    assert set(by_id) == {"xtts", "piper"}
    assert body["contract_engines"] == []
    assert by_id["piper"]["status"]["status"] == "ok"

    activated = client.post(
        "/v1/admin/tts/activate", json={"engine": "piper"}, headers=admin_headers
    ).json()
    assert activated["active_engine"] == "piper"
    assert "warning" not in activated
    assert client.get("/v1/admin/tts", headers=admin_headers).json()["active_engine"] == "piper"


def test_activating_unreachable_engine_warns_but_is_allowed(client, admin_headers, monkeypatch):
    """Vorab waehlen ist erlaubt - der Admin erfaehrt aber sofort, dass bis
    zum Start des Containers XTTS spricht."""
    _ok_status(monkeypatch, status="unreachable")

    body = client.post(
        "/v1/admin/tts/activate", json={"engine": "piper"}, headers=admin_headers
    ).json()

    assert body["active_engine"] == "piper"
    assert "XTTS" in body["warning"]

    # Auch XTTS selbst meldet, wenn es gerade nicht antwortet
    body = client.post(
        "/v1/admin/tts/activate", json={"engine": "xtts"}, headers=admin_headers
    ).json()
    assert "App" in body["warning"]


def test_activating_unknown_engine_is_rejected(client, admin_headers):
    response = client.post(
        "/v1/admin/tts/activate", json={"engine": "elevenlabs"}, headers=admin_headers
    )
    assert response.status_code == 404
    assert tts_engines.active_engine() == "xtts"


async def test_unknown_host_says_the_container_is_missing(monkeypatch):
    """Nutzer-Report: "[Errno -2] Name or service not known" beim Aktivieren
    sah wie ein Programmfehler aus - gemeint ist: der Container laeuft
    nicht. Die Meldung sagt jetzt genau das und was zu tun ist."""

    def handler(request):
        try:
            raise socket.gaierror(-2, "Name or service not known")
        except socket.gaierror as cause:
            raise httpx.ConnectError("[Errno -2] Name or service not known",
                                     request=request) from cause

    _mock_http(monkeypatch, handler)

    status = await tts_engines.engine_status("xtts")

    assert status["status"] == "unreachable"
    assert "Server nicht gefunden" in status["detail"]
    assert "'tts-xtts'" in status["detail"]
    assert "Voice-Stack" in status["detail"]


async def test_refused_connection_points_to_the_logs(monkeypatch):
    def handler(request):
        try:
            raise ConnectionRefusedError(111, "Connect call failed")
        except ConnectionRefusedError as cause:
            raise httpx.ConnectError("All connection attempts failed", request=request) from cause

    _mock_http(monkeypatch, handler)

    status = await tts_engines.engine_status("xtts")

    assert status["status"] == "unreachable"
    assert "docker logs heimai-tts-xtts" in status["detail"]


async def test_timeout_is_named_as_such(monkeypatch):
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    _mock_http(monkeypatch, handler)

    status = await tts_engines.engine_status("xtts")

    assert status == {"status": "unreachable",
                      "detail": "'tts-xtts' antwortet nicht innerhalb von 3 s."}


# ---- Adressen aus dem Panel (audio.cpp, Vertrags-Engines) ---------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("", ""),
    ("   ", ""),
    ("http://audiocpp:8080", "http://audiocpp:8080"),
    ("192.168.2.105:8080/", "http://192.168.2.105:8080"),
    ("audiocpp.example.org", "http://audiocpp.example.org"),
    ("https://ai.example.org/audiocpp/", "https://ai.example.org/audiocpp"),
])
def test_base_url_is_normalized(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "ftp://audiocpp:21", "http://", "http://audiocpp:99999", "http://audiocpp:8080/?x=1",
])
def test_invalid_base_url_is_rejected(raw):
    with pytest.raises(ValueError):
        normalize_base_url(raw)


def test_tts_routes_require_admin(client):
    assert client.get("/v1/admin/tts").status_code == 401
    assert client.post("/v1/admin/tts/activate", json={"engine": "piper"}).status_code == 401


# ---- Sprach-Turn -------------------------------------------------------------------


def test_voice_turn_is_spoken_by_active_engine(client, monkeypatch):
    xtts_calls: list = []
    monkeypatch.setattr(piper_client, "stream", _fake_engine(b"\x0b\x0b"))
    monkeypatch.setattr(xtts_client, "stream", _fake_engine(b"\x0a\x0a", xtts_calls))
    tts_engines.set_active_engine("piper")

    frames = _voice_turn(client, monkeypatch)

    assert _spoken(frames) == b"\x0b\x0b" * 1200
    assert xtts_calls == []
    assert [f["type"] for f in frames][-2:] == ["audio_end", "done"]


def test_failing_test_engine_falls_back_to_xtts(client, monkeypatch):
    """Container der Engine gestoppt/laedt noch -> XTTS spricht, der Nutzer
    bekommt trotzdem eine Sprachantwort."""

    async def stopped(text, voice_id=None, language=None):
        raise httpx.ConnectError("Name or service not known")
        yield  # pragma: no cover

    tts_engines.set_active_engine(_demo_engine(monkeypatch, stopped))
    monkeypatch.setattr(xtts_client, "stream", _fake_engine(b"\x0a\x0a"))

    frames = _voice_turn(client, monkeypatch)

    assert _spoken(frames) == b"\x0a\x0a" * 1200
    assert "error" not in [f["type"] for f in frames]


def test_no_fallback_once_audio_is_flowing(client, monkeypatch):
    """Nach dem ersten Chunk waere ein Wechsel doppeltes Audio - dann bleibt
    es beim Teil-Audio, der Turn endet trotzdem sauber."""
    xtts_calls: list = []

    async def breaks_midway(text, voice_id=None, language=None):
        yield 24000, b"\x0b\x0b" * 1200
        raise httpx.RemoteProtocolError("peer closed connection")

    tts_engines.set_active_engine(_demo_engine(monkeypatch, breaks_midway))
    monkeypatch.setattr(xtts_client, "stream", _fake_engine(b"\x0a\x0a", xtts_calls))

    frames = _voice_turn(client, monkeypatch)

    assert _spoken(frames) == b"\x0b\x0b" * 1200
    assert xtts_calls == []
    assert frames[-1]["type"] == "done"


def _xtts_off():
    repos.set_setting("gpu_device_tts-xtts", "off")


def test_piper_steps_in_while_xtts_is_switched_off(client, monkeypatch):
    """XTTS im Panel aus (v1.19) und die aktive Engine faellt aus -> Piper
    spricht, XTTS wird gar nicht erst gefragt."""
    xtts_calls: list = []

    async def stopped(text, voice_id=None, language=None):
        raise httpx.ConnectError("Name or service not known")
        yield  # pragma: no cover

    tts_engines.set_active_engine(_demo_engine(monkeypatch, stopped))
    _xtts_off()
    monkeypatch.setattr(xtts_client, "stream", _fake_engine(b"\x0a\x0a", xtts_calls))
    monkeypatch.setattr(piper_client, "stream", _fake_engine(b"\x0c\x0c"))

    frames = _voice_turn(client, monkeypatch)

    assert _spoken(frames) == b"\x0c\x0c" * 1200
    assert xtts_calls == []
    assert tts_engines.fallback_engine() == "piper"


def test_activating_a_switched_off_xtts_turns_it_back_on(client, admin_headers, monkeypatch):
    """v1.20: Aktivieren heisst "diese Engine soll sprechen" - eine
    ausgeschaltete wird dafuer wieder auf ihre letzte Karte geladen."""
    from orchestrator.services import gpu_manager

    tts_engines.set_active_engine("piper")
    _xtts_off()
    repos.set_setting("gpu_last_device_tts-xtts", "cuda:1")
    loads = []

    async def status(name):
        return {"reachable": True, "assigned": "off", "effective": None, "loaded": False}

    async def set_device(name, device, compute_type=None):
        loads.append((name, device))
        return {"assigned": device, "effective": device, "loaded": True}

    monkeypatch.setattr(gpu_manager, "service_status", status)
    monkeypatch.setattr(gpu_manager, "set_service_device", set_device)

    response = client.post("/v1/admin/tts/activate", json={"engine": "xtts"}, headers=admin_headers)

    assert response.status_code == 200
    assert tts_engines.active_engine() == "xtts"
    assert loads == [("tts-xtts", "cuda:1")]
    assert gpu_manager.assigned_device("tts-xtts") == "cuda:1"
    assert "XTTS geladen auf cuda:1." in response.json()["notes"]


async def test_switched_off_xtts_reports_off_without_asking_the_service():
    _xtts_off()

    assert (await tts_engines.engine_status("xtts"))["status"] == "off"


async def test_preview_and_fillers_leave_a_switched_off_xtts_alone(client, admin_headers, monkeypatch):
    tts_engines.set_active_engine("piper")
    _xtts_off()
    calls: list = []
    monkeypatch.setattr(xtts_client, "stream", _fake_engine(b"\x03\x04", calls))

    async def no_synthesis(*args, **kwargs):
        calls.append(args)
        return b"", 24000

    monkeypatch.setattr(xtts_client, "synthesize", no_synthesis)

    preview = client.post("/v1/admin/tts/preview", headers=admin_headers,
                          json={"engine": "xtts", "voice_id": "default-de-male", "text": "Hallo"})
    trigger = repos.create_trigger("T-xtts-off", "thinking", None)
    filler = repos.create_filler("Titel", "Moment bitte.", trigger["id"], True,
                                 delay_ms=0, engine="xtts")
    results = await filler_service.generate_audio(filler["id"])

    assert preview.status_code == 409
    assert "ausgeschaltet" in preview.json()["detail"]
    assert results and all(not r["ok"] and "ausgeschaltet" in r["error"] for r in results)
    assert calls == []


def test_piper_can_speak_the_main_answer(client, monkeypatch):
    """Piper liefert 22,05 kHz am Stueck - fuer den Stream auf 24 kHz
    gebracht und in 1-s-Chunks geteilt."""
    monkeypatch.setattr(
        piper_client, "synthesize", AsyncMock(return_value=(b"\x05\x06" * 33075, 22050))
    )
    tts_engines.set_active_engine("piper")

    frames = _voice_turn(client, monkeypatch)

    audio = [f for f in frames if f["type"] == "audio_chunk"]
    assert len(audio) == 2  # 1,5 s Audio -> 1 s + 0,5 s
    assert all(f["sample_rate"] == 24000 for f in audio)
    assert len(_spoken(frames)) == pytest.approx(1.5 * 24000 * 2, abs=8)


# ---- Abgerissener Stream (Server stuerzt mitten in der Synthese ab) -------------------------


def _mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_with_transport(*args, **kwargs):
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_transport)


def _crashing_server() -> str:
    """Echter Socket-Server wie eine Engine bei einem CUDA-Absturz: Header
    (chunked) sind raus, dann endet der Prozess mitten im Stream."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        with conn, server:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: audio/pcm\r\n"
                         b"X-Sample-Rate: 24000\r\nTransfer-Encoding: chunked\r\n\r\n")

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{server.getsockname()[1]}"


def test_preview_explains_an_aborted_stream(client, admin_headers, monkeypatch):
    engine = _demo_engine(monkeypatch, url=_crashing_server())

    response = client.post(
        "/v1/admin/tts/preview",
        json={"engine": engine, "voice_id": "default-de-female", "text": "Hallo"},
        headers=admin_headers,
    )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "mitten in der Synthese abgebrochen" in detail
    assert "docker logs heimai-tts-demo" in detail and "nvidia-smi" in detail
    assert "incomplete chunked read" not in detail


async def test_filler_names_an_aborted_stream(client, monkeypatch):
    async def aborted(text, voice_id=None, language=None):
        raise httpx.ReadError("[Errno 104] Connection reset by peer")
        yield  # pragma: no cover

    engine = _demo_engine(monkeypatch, aborted)
    _write_sample("default-de-male")
    trigger = repos.create_trigger("T-abort", "thinking", None)
    filler = repos.create_filler("Titel", "Moment bitte.", trigger["id"], True,
                                 delay_ms=0, engine=engine)

    results = await filler_service.generate_audio(filler["id"], "default-de-male")

    assert results[0]["ok"] is False
    assert "mitten in der Synthese abgebrochen" in results[0]["error"]
    assert "Piper" in results[0]["error"]


# ---- Probehoeren -------------------------------------------------------------------


def test_preview_returns_wav_with_timing(client, admin_headers, monkeypatch):
    calls: list = []
    monkeypatch.setattr(xtts_client, "stream", _fake_engine(b"\x03\x04", calls))

    response = client.post(
        "/v1/admin/tts/preview",
        json={"engine": "xtts", "voice_id": "default-de-male", "text": "  Hallo!  "},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert calls == [("Hallo!", "default-de-male")]
    with wave.open(io.BytesIO(response.content), "rb") as wav:
        assert wav.getframerate() == 24000
        assert wav.readframes(wav.getnframes()) == b"\x03\x04" * 1200
    assert int(response.headers["x-audio-ms"]) == 50  # 1200 Samples @ 24 kHz
    assert int(response.headers["x-tts-total-ms"]) >= int(response.headers["x-tts-first-chunk-ms"])


def test_preview_uses_exactly_the_chosen_engine(client, admin_headers, monkeypatch):
    """Kein Fallback beim Probehoeren: ein Engine-Fehler kommt als Fehler an,
    statt still XTTS abzuspielen."""

    async def broken(text, voice_id=None, language=None):
        raise RuntimeError("Engine-Fehler 500: CUDA out of memory")
        yield  # pragma: no cover

    xtts_calls: list = []
    engine = _demo_engine(monkeypatch, broken)
    monkeypatch.setattr(xtts_client, "stream", _fake_engine(b"\x03\x04", xtts_calls))

    response = client.post(
        "/v1/admin/tts/preview",
        json={"engine": engine, "voice_id": "default-de-female", "text": "Hallo"},
        headers=admin_headers,
    )

    assert response.status_code == 502
    assert "CUDA out of memory" in response.json()["detail"]
    assert xtts_calls == []


def test_preview_rejects_unknown_engine_and_empty_text(client, admin_headers):
    assert client.post(
        "/v1/admin/tts/preview",
        json={"engine": "elevenlabs", "voice_id": "default-de-female", "text": "Hallo"},
        headers=admin_headers,
    ).status_code == 404
    assert client.post(
        "/v1/admin/tts/preview",
        json={"engine": "xtts", "voice_id": "default-de-female", "text": ""},
        headers=admin_headers,
    ).status_code == 422
    assert client.post(
        "/v1/admin/tts/preview",
        json={"engine": "xtts", "voice_id": "default-de-female", "text": "   "},
        headers=admin_headers,
    ).status_code == 400


# ---- Filler mit weiteren Engines -------------------------------------------------------


async def test_fillers_are_generated_per_voice_without_sample(client, monkeypatch):
    """Engines, die kein Sample brauchen (Steckbrief), sprechen ohne mit
    ihrer eingebauten Stimme - pro Stimme ein Audio."""
    calls: list = []
    engine = _demo_engine(monkeypatch, _fake_engine(b"\x09\x09", calls))
    tts_engines._INFO[engine] = {"needs_sample": False}
    trigger = repos.create_trigger("T-ohne-sample", "thinking", None)
    filler = repos.create_filler("Titel", "Moment bitte.", trigger["id"], True,
                                 delay_ms=0, engine=engine)

    results = await filler_service.generate_audio(filler["id"])

    assert all(r["ok"] for r in results)
    assert {voice_id for _, voice_id in calls} == {v["id"] for v in repos.list_voices()}
    for voice in repos.list_voices():
        assert filler_service.has_audio(filler["id"], voice["id"])


async def test_filler_runs_through_the_shared_preparation(client, monkeypatch):
    """Filler weiterer Engines bekommen dieselbe Aufbereitung wie XTTS (v1.16):
    sauberes Satzende, getrimmte Stille - und mit voice_id nur diese eine
    Stimme."""
    silence = b"\x00\x00" * 4800  # 0,2 s
    tone = b"".join(int(8000 * math.sin(i / 8)).to_bytes(2, "little", signed=True)
                    for i in range(24000))  # 1 s, plausibel fuer 13 Zeichen
    calls: list = []

    async def fake_engine(text, voice_id=None, language=None):
        calls.append((text, voice_id))
        yield 24000, silence + tone + silence

    engine = _demo_engine(monkeypatch, fake_engine)
    _write_sample("default-de-male")
    trigger = repos.create_trigger("T-aufbereitung", "thinking", None)
    filler = repos.create_filler("Titel", "Moment bitte...", trigger["id"], True,
                                 delay_ms=0, engine=engine)

    results = await filler_service.generate_audio(filler["id"], "default-de-male")

    assert calls == [("Moment bitte.", "default-de-male")]
    assert [r["voice_id"] for r in results] == ["default-de-male"]
    assert results[0]["ok"] is True and "warning" not in results[0]
    assert not filler_service.has_audio(filler["id"], "default-de-female")
    pcm, rate = filler_service.load_audio(filler_service.audio_path(filler["id"], "default-de-male"))
    assert rate == 24000
    assert len(pcm) < len(silence + tone + silence)  # Stille vorn/hinten gekappt


def test_filler_can_be_created_with_another_engine(client, admin_headers, monkeypatch):
    engine = _demo_engine(monkeypatch)
    trigger = client.get("/v1/admin/triggers", headers=admin_headers).json()[0]
    response = client.post(
        "/v1/admin/fillers",
        json={"title": "x", "text": "y", "trigger_id": trigger["id"], "engine": engine},
        headers=admin_headers,
    )
    assert response.status_code == 201
    assert response.json()["engine"] == engine
    # Ausgebaute Engines (Breeze, v1.22) werden abgelehnt
    assert client.post(
        "/v1/admin/fillers",
        json={"title": "x", "text": "y", "trigger_id": trigger["id"], "engine": "breeze"},
        headers=admin_headers,
    ).status_code == 422


# ---- Stimmen: Sample-Transkript --------------------------------------------------------


def test_sample_upload_proposes_transcript(client, admin_headers, monkeypatch):
    transcribe = AsyncMock(return_value=" Hallo, das ist meine Stimme. ")
    monkeypatch.setattr(stream_module.stt_client, "transcribe", transcribe)

    upload = client.post(
        "/v1/admin/voices/default-de-female/sample",
        files={"file": ("sample.wav", _wav_bytes(rate=22050), "audio/wav")},
        headers=admin_headers,
    )

    assert upload.status_code == 200
    assert upload.json()["sample_text"] == "Hallo, das ist meine Stimme."
    # Whisper bekommt PCM16 + die echte Rate des Samples
    assert transcribe.await_args.args[1] == 22050
    voices = {v["id"]: v for v in client.get("/v1/admin/voices", headers=admin_headers).json()}
    assert voices["default-de-female"]["sample_text"] == "Hallo, das ist meine Stimme."


def test_failed_transcription_keeps_the_upload(client, admin_headers):
    """STT nicht erreichbar (conftest) -> Sample trotzdem gespeichert, altes
    Transkript verworfen (passt nicht zum neuen Sample)."""
    repos.update_voice("default-de-female", {"sample_text": "Altes Transkript."})

    upload = client.post(
        "/v1/admin/voices/default-de-female/sample",
        files={"file": ("sample.wav", _wav_bytes(), "audio/wav")},
        headers=admin_headers,
    )

    assert upload.status_code == 200
    assert upload.json()["sample_text"] is None
    assert "nicht verfuegbar" in upload.json()["transcript_error"]
    assert repos.get_voice("default-de-female")["sample_text"] is None


def test_transcript_can_be_edited_and_stays_internal(client, admin_headers):
    updated = client.put(
        "/v1/admin/voices/default-de-male",
        json={"sample_text": "  Korrigierter Wortlaut.  "},
        headers=admin_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["sample_text"] == "Korrigierter Wortlaut."
    assert updated.json()["name"] == "Standard (maennlich, DE)"  # unveraendert

    # Leeren = kein Transkript
    cleared = client.put(
        "/v1/admin/voices/default-de-male", json={"sample_text": ""}, headers=admin_headers
    )
    assert cleared.json()["sample_text"] is None

    # Die App-Schnittstelle (PROTOCOL.md) bleibt unveraendert
    public = client.get("/v1/voices").json()["voices"]
    assert all(set(voice) == {"id", "name", "language"} for voice in public)

    assert client.put(
        "/v1/admin/voices/gibtsnicht", json={"name": "x"}, headers=admin_headers
    ).status_code == 404


def test_transcribe_existing_sample(client, admin_headers, monkeypatch):
    # Ohne Sample gibt es nichts zu transkribieren
    assert client.post(
        "/v1/admin/voices/default-de-male/transcribe", headers=admin_headers
    ).status_code == 400

    _write_sample("default-de-male")
    monkeypatch.setattr(
        stream_module.stt_client, "transcribe", AsyncMock(return_value="Guten Tag.")
    )
    response = client.post("/v1/admin/voices/default-de-male/transcribe", headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["sample_text"] == "Guten Tag."
    assert response.json()["has_sample"] is True


def test_transcribe_failure_keeps_existing_transcript(client, admin_headers):
    _write_sample("default-de-male")
    repos.update_voice("default-de-male", {"sample_text": "Bleibt stehen."})

    response = client.post("/v1/admin/voices/default-de-male/transcribe", headers=admin_headers)

    assert response.status_code == 502
    assert repos.get_voice("default-de-male")["sample_text"] == "Bleibt stehen."


# ---- Ausbau von Breeze und Qwen3-TTS (v1.22) --------------------------------------------


def _old_database(**settings_values: str) -> None:
    """Stand vor v1.22 nachstellen: vorbelegtes Qwen3, Breeze-Einstellungen."""
    from orchestrator.db import db_session

    with db_session() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = 'retired_breeze_qwen3'")
        conn.execute("INSERT INTO tts_contract_engines (id, url, label)"
                     " VALUES ('qwen3', 'http://tts-qwen3:8000', 'Qwen3-TTS')")
        for key, value in {"breeze_base_url": "http://tts-breeze-cpp:7860",
                           "breeze_instruction": "Speak calmly.", **settings_values}.items():
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()


def test_update_removes_breeze_and_the_preset_qwen3_once():
    from orchestrator.db import init_db
    from orchestrator.services import gpu_manager

    _old_database(**{"tts_engine": "qwen3", "gpu_device_tts-xtts": "off",
                     "gpu_last_device_tts-xtts": "cuda:1", "gpu_auto_off_tts-xtts": "qwen3"})

    init_db()

    assert repos.list_contract_engines() == []
    assert repos.get_setting("breeze_base_url") is None and repos.get_setting("breeze_instruction") is None
    # Die alte Hauptstimme gibt es nicht mehr -> XTTS, wieder auf seiner Karte
    assert repos.get_setting(tts_engines.ACTIVE_ENGINE_SETTING) is None
    assert tts_engines.active_engine() == "xtts"
    assert gpu_manager.assigned_device("tts-xtts") == "cuda:1" and not gpu_manager.auto_off("tts-xtts")

    # Einmalig: eine danach bewusst eingetragene Engine "qwen3" bleibt.
    repos.upsert_contract_engine("qwen3", "http://tts-qwen3:8000", "Qwen3-TTS")
    init_db()
    assert [e["id"] for e in repos.list_contract_engines()] == ["qwen3"]


def test_update_keeps_a_qwen3_entry_the_admin_pointed_elsewhere():
    from orchestrator.db import db_session, init_db

    _old_database(tts_engine="audiocpp:qwen3-tts")
    with db_session() as conn:
        conn.execute("UPDATE tts_contract_engines SET url = 'http://192.168.2.50:8000' WHERE id = 'qwen3'")
        conn.commit()

    init_db()

    assert [e["url"] for e in repos.list_contract_engines()] == ["http://192.168.2.50:8000"]
    assert repos.get_setting(tts_engines.ACTIVE_ENGINE_SETTING) == "audiocpp:qwen3-tts"
