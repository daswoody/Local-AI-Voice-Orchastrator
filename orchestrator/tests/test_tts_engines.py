"""TTS-Engine-Wechsel der Hauptstimme + Breeze TTS 2 als Test-Engine (v1.17)."""

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
    BREEZE_INSTRUCTION_SETTING,
    BREEZE_URL_SETTING,
    breeze_client,
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
    tts_engines.set_active_engine("breeze")
    assert tts_engines.active_engine() == "breeze"


def test_unknown_stored_engine_falls_back_to_xtts():
    """Z. B. eine Engine, die es in einer spaeteren Version nicht mehr gibt."""
    repos.set_setting(tts_engines.ACTIVE_ENGINE_SETTING, "gibtsnicht")
    assert tts_engines.active_engine() == "xtts"


def test_admin_overview_and_activation(client, admin_headers, monkeypatch):
    _ok_status(monkeypatch)

    body = client.get("/v1/admin/tts", headers=admin_headers).json()
    assert body["active_engine"] == "xtts"
    by_id = {engine["id"]: engine for engine in body["engines"]}
    assert set(by_id) == {"xtts", "piper", "breeze", "qwen3"}
    assert body["contract_engines"] == [
        {"id": "qwen3", "url": "http://tts-qwen3:8000", "label": "Qwen3-TTS"}]
    assert by_id["breeze"]["status"]["status"] == "ok"

    activated = client.post(
        "/v1/admin/tts/activate", json={"engine": "breeze"}, headers=admin_headers
    ).json()
    assert activated["active_engine"] == "breeze"
    assert "warning" not in activated
    assert client.get("/v1/admin/tts", headers=admin_headers).json()["active_engine"] == "breeze"


def test_activating_unreachable_engine_warns_but_is_allowed(client, admin_headers, monkeypatch):
    """Vorab waehlen ist erlaubt - der Admin erfaehrt aber sofort, dass bis
    zum Start des Containers XTTS spricht."""
    _ok_status(monkeypatch, status="unreachable")

    body = client.post(
        "/v1/admin/tts/activate", json={"engine": "breeze"}, headers=admin_headers
    ).json()

    assert body["active_engine"] == "breeze"
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
    sah wie ein Programmfehler aus - gemeint ist: Breeze-Container laeuft
    nicht. Die Meldung sagt jetzt genau das und was zu tun ist."""

    def handler(request):
        try:
            raise socket.gaierror(-2, "Name or service not known")
        except socket.gaierror as cause:
            raise httpx.ConnectError("[Errno -2] Name or service not known",
                                     request=request) from cause

    _mock_http(monkeypatch, handler)

    status = await tts_engines.engine_status("breeze")

    assert status["status"] == "unreachable"
    assert "Server nicht gefunden" in status["detail"]
    assert "'tts-breeze'" in status["detail"]
    assert "docker-compose.breeze.yml" in status["detail"]
    assert "docker-compose.breeze-cpp.yml" in status["detail"]


async def test_refused_connection_points_to_the_logs(monkeypatch):
    def handler(request):
        try:
            raise ConnectionRefusedError(111, "Connect call failed")
        except ConnectionRefusedError as cause:
            raise httpx.ConnectError("All connection attempts failed", request=request) from cause

    _mock_http(monkeypatch, handler)

    status = await tts_engines.engine_status("breeze")

    assert status["status"] == "unreachable"
    assert "docker logs heimai-tts-breeze" in status["detail"]
    assert "breeze-server" in status["detail"]  # auch der native Weg


async def test_timeout_is_named_as_such(monkeypatch):
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    _mock_http(monkeypatch, handler)

    status = await tts_engines.engine_status("xtts")

    assert status == {"status": "unreachable",
                      "detail": "'tts-xtts' antwortet nicht innerhalb von 3 s."}


# ---- Breeze-Server-Adresse (v1.18) ------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("", ""),
    ("   ", ""),
    ("http://tts-breeze-cpp:7860", "http://tts-breeze-cpp:7860"),
    ("192.168.2.105:7860/", "http://192.168.2.105:7860"),
    ("breeze.example.org", "http://breeze.example.org"),
    ("https://ai.example.org/breeze/", "https://ai.example.org/breeze"),
])
def test_breeze_url_is_normalized(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "ftp://breeze:21", "http://", "http://breeze:99999", "http://breeze:7860/?x=1",
])
def test_invalid_breeze_url_is_rejected(raw):
    with pytest.raises(ValueError):
        normalize_base_url(raw)


def test_breeze_url_can_be_set_and_reset_in_the_panel(client, admin_headers, monkeypatch):
    _ok_status(monkeypatch)
    body = client.get("/v1/admin/tts", headers=admin_headers).json()
    assert body["breeze_url"] == ""
    assert body["breeze_url_effective"] == "http://tts-breeze:7860"  # .env-Standard

    saved = client.put("/v1/admin/tts/settings", json={"breeze_url": "192.168.2.105:7860"},
                       headers=admin_headers)
    assert saved.status_code == 200
    assert saved.json()["breeze_url_effective"] == "http://192.168.2.105:7860"
    # Nur mitgeschickte Felder aendern sich
    client.put("/v1/admin/tts/settings", json={"breeze_instruction": "Speak calmly."},
               headers=admin_headers)
    body = client.get("/v1/admin/tts", headers=admin_headers).json()
    assert body["breeze_url"] == "http://192.168.2.105:7860"
    assert body["breeze_instruction"] == "Speak calmly."

    # Leeren = zurueck zur .env
    client.put("/v1/admin/tts/settings", json={"breeze_url": ""}, headers=admin_headers)
    body = client.get("/v1/admin/tts", headers=admin_headers).json()
    assert body["breeze_url_effective"] == "http://tts-breeze:7860"


def test_invalid_breeze_url_is_refused_and_keeps_the_old_one(client, admin_headers):
    client.put("/v1/admin/tts/settings", json={"breeze_url": "http://tts-breeze-cpp:7860"},
               headers=admin_headers)
    response = client.put("/v1/admin/tts/settings", json={"breeze_url": "ftp://x"},
                          headers=admin_headers)
    assert response.status_code == 400
    assert repos.get_setting(BREEZE_URL_SETTING) == "http://tts-breeze-cpp:7860"


async def test_breeze_client_and_status_use_the_configured_url(monkeypatch):
    repos.set_setting(BREEZE_URL_SETTING, "http://breeze.lan:8137/prefix")
    urls: list[str] = []

    def handler(request):
        urls.append(str(request.url))
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok", "sample_rate": 24000})
        return httpx.Response(200, headers={"x-sample-rate": "24000"}, content=b"\x01\x02")

    _mock_http(monkeypatch, handler)

    [c async for c in breeze_client.stream("Hallo.", "default-de-female")]
    status = await tts_engines.engine_status("breeze")

    assert urls == ["http://breeze.lan:8137/prefix/v1/audio/speech",
                    "http://breeze.lan:8137/prefix/health"]
    assert status["status"] == "ok"


def test_tts_routes_require_admin(client):
    assert client.get("/v1/admin/tts").status_code == 401
    assert client.post("/v1/admin/tts/activate", json={"engine": "piper"}).status_code == 401


# ---- Sprach-Turn -------------------------------------------------------------------


def test_voice_turn_is_spoken_by_active_engine(client, monkeypatch):
    xtts_calls: list = []
    monkeypatch.setattr(breeze_client, "stream", _fake_engine(b"\x0b\x0b"))
    monkeypatch.setattr(xtts_client, "stream", _fake_engine(b"\x0a\x0a", xtts_calls))
    tts_engines.set_active_engine("breeze")

    frames = _voice_turn(client, monkeypatch)

    assert _spoken(frames) == b"\x0b\x0b" * 1200
    assert xtts_calls == []
    assert [f["type"] for f in frames][-2:] == ["audio_end", "done"]


def test_failing_test_engine_falls_back_to_xtts(client, monkeypatch):
    """Breeze-Container gestoppt/laedt noch -> XTTS spricht, der Nutzer
    bekommt trotzdem eine Sprachantwort."""

    async def stopped(text, voice_id=None, language=None):
        raise httpx.ConnectError("Name or service not known")
        yield  # pragma: no cover

    monkeypatch.setattr(breeze_client, "stream", stopped)
    monkeypatch.setattr(xtts_client, "stream", _fake_engine(b"\x0a\x0a"))
    tts_engines.set_active_engine("breeze")

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

    monkeypatch.setattr(breeze_client, "stream", breaks_midway)
    monkeypatch.setattr(xtts_client, "stream", _fake_engine(b"\x0a\x0a", xtts_calls))
    tts_engines.set_active_engine("breeze")

    frames = _voice_turn(client, monkeypatch)

    assert _spoken(frames) == b"\x0b\x0b" * 1200
    assert xtts_calls == []
    assert frames[-1]["type"] == "done"


def _xtts_off():
    repos.set_setting("gpu_device_tts-xtts", "off")


def test_piper_steps_in_while_xtts_is_switched_off(client, monkeypatch):
    """XTTS im Panel aus (v1.19) und Breeze faellt aus -> Piper spricht,
    XTTS wird gar nicht erst gefragt."""
    xtts_calls: list = []

    async def stopped(text, voice_id=None, language=None):
        raise httpx.ConnectError("Name or service not known")
        yield  # pragma: no cover

    tts_engines.set_active_engine("breeze")
    _xtts_off()
    monkeypatch.setattr(breeze_client, "stream", stopped)
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

    tts_engines.set_active_engine("breeze")
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
    tts_engines.set_active_engine("breeze")
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


# ---- Breeze-Client (Multipart gegen den offiziellen Server) ---------------------------


def _mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_with_transport(*args, **kwargs):
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_transport)


async def test_breeze_clones_voice_from_sample_and_transcript(monkeypatch):
    _write_sample("default-de-female")
    repos.update_voice("default-de-female", {"sample_text": "Das ist mein Sample."})
    repos.set_setting(BREEZE_INSTRUCTION_SETTING, "Speak calmly.")
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, headers={"x-sample-rate": "24000", "x-sample-format": "s16le"},
            content=b"\x01\x02" * 100,
        )

    _mock_http(monkeypatch, handler)

    chunks = [c async for c in breeze_client.stream("Hallo Welt.", "default-de-female")]

    assert chunks == [(24000, b"\x01\x02" * 100)]
    request = requests[0]
    assert request.url.path == "/v1/audio/speech"
    body = request.content
    assert b'name="text"' in body and "Hallo Welt.".encode() in body
    assert b'name="ref_audio"; filename="default-de-female.wav"' in body
    assert b'name="ref_text"' in body and "Das ist mein Sample.".encode() in body
    assert b'name="instruction"' in body and b"Speak calmly." in body
    assert b'name="cfg_scale"' in body


async def test_breeze_without_transcript_uses_its_builtin_voice(monkeypatch):
    """Sample ohne Transkript reicht nicht (der Server verlangt beides) -
    dann ohne Referenz, statt mit falschem Transkript zu klonen."""
    _write_sample("default-de-female")
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"x-sample-rate": "24000"}, content=b"\x01\x02")

    _mock_http(monkeypatch, handler)

    [c async for c in breeze_client.stream("Hallo.", "default-de-female")]

    body = requests[0].content
    assert b"ref_audio" not in body and b"ref_text" not in body
    assert b"instruction" not in body


async def test_breeze_waits_while_the_server_is_busy(monkeypatch):
    """Der Breeze-Server nimmt nur einen Request zur Zeit (409) - z. B. wenn
    parallel Filler generiert werden."""
    monkeypatch.setattr(breeze_client, "busy_wait_s", 0)
    answers = iter([
        httpx.Response(409, json={"detail": "An inference request is already running."}),
        httpx.Response(200, headers={"x-sample-rate": "24000"}, content=b"\x01\x02"),
    ])
    _mock_http(monkeypatch, lambda request: next(answers))

    chunks = [c async for c in breeze_client.stream("Hallo.", "default-de-female")]

    assert chunks == [(24000, b"\x01\x02")]


def _multipart_file(request: httpx.Request, field: str) -> bytes:
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    for part in request.content.split(b"--" + boundary):
        head, _, body = part.partition(b"\r\n\r\n")
        if f'name="{field}"'.encode() in head:
            return body.removesuffix(b"\r\n")
    raise AssertionError(f"kein Feld {field} im Request")


async def test_breeze_gets_the_sample_as_mono_pcm16_24k(monkeypatch):
    """Breeze-TTS-2.cpp liest nur 16/32-Bit-WAVs - ein 24-Bit-Sample kaeme
    dort als Stille an. Der Client schickt deshalb immer mono PCM16, 24 kHz."""
    path = Path(settings.voices_dir) / "default-de-female.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = b"".join(
        int(0.5 * 8388607 * math.sin(2 * math.pi * 220 * i / 48000)).to_bytes(3, "little", signed=True) * 2
        for i in range(48000)
    )  # 1 s Ton, 24 Bit, stereo, 48 kHz
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(3)
        wav.setframerate(48000)
        wav.writeframes(frames)
    repos.update_voice("default-de-female", {"sample_text": "Das ist mein Sample."})
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"x-sample-rate": "24000"}, content=b"\x01\x02")

    _mock_http(monkeypatch, handler)

    [c async for c in breeze_client.stream("Hallo.", "default-de-female")]

    with wave.open(io.BytesIO(_multipart_file(requests[0], "ref_audio")), "rb") as ref:
        assert (ref.getnchannels(), ref.getsampwidth(), ref.getframerate()) == (1, 2, 24000)
        pcm = ref.readframes(ref.getnframes())
    assert abs(len(pcm) / 2 / 24000 - 1.0) < 0.01
    samples = [int.from_bytes(pcm[i:i + 2], "little", signed=True) for i in range(0, len(pcm), 2)]
    assert max(samples) > 12000  # Ton kommt an (0,5 Vollaussteuerung), keine Stille


def _crashing_breeze_server() -> str:
    """Echter Socket-Server wie Breeze-TTS-2.cpp bei einem CUDA-Absturz:
    Header (chunked) sind raus, dann endet der Prozess mitten im Stream."""
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


def test_preview_explains_an_aborted_breeze_stream(client, admin_headers):
    repos.set_setting(BREEZE_URL_SETTING, _crashing_breeze_server())

    response = client.post(
        "/v1/admin/tts/preview",
        json={"engine": "breeze", "voice_id": "default-de-female", "text": "Hallo"},
        headers=admin_headers,
    )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "mitten in der Synthese abgebrochen" in detail
    assert "docker logs heimai-tts-breeze" in detail and "BREEZE_CUDA_ARCHS" in detail
    assert "incomplete chunked read" not in detail


async def test_breeze_filler_names_an_aborted_stream(client, monkeypatch):
    trigger = repos.create_trigger("T-breeze-abort", "thinking", None)
    filler = repos.create_filler("Titel", "Moment bitte.", trigger["id"], True,
                                 delay_ms=0, engine="breeze")

    async def aborted(text, voice_id=None, language=None):
        raise httpx.ReadError("[Errno 104] Connection reset by peer")
        yield  # pragma: no cover

    monkeypatch.setattr(breeze_client, "stream", aborted)

    results = await filler_service.generate_audio(filler["id"], "default-de-male")

    assert results[0]["ok"] is False
    assert "mitten in der Synthese abgebrochen" in results[0]["error"]
    assert "Piper" in results[0]["error"]


async def test_breeze_error_carries_server_detail(monkeypatch):
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(400, json={"detail": "cfg_scale must be greater than 0."}),
    )

    with pytest.raises(RuntimeError, match="Breeze-Fehler 400.*cfg_scale"):
        [c async for c in breeze_client.stream("Hallo.", "default-de-female")]


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
    """Kein Fallback beim Probehoeren: ein Breeze-Fehler kommt als Fehler an,
    statt still XTTS abzuspielen."""

    async def broken(text, voice_id=None, language=None):
        raise RuntimeError("Breeze-Fehler 500: CUDA out of memory")
        yield  # pragma: no cover

    xtts_calls: list = []
    monkeypatch.setattr(breeze_client, "stream", broken)
    monkeypatch.setattr(xtts_client, "stream", _fake_engine(b"\x03\x04", xtts_calls))

    response = client.post(
        "/v1/admin/tts/preview",
        json={"engine": "breeze", "voice_id": "default-de-female", "text": "Hallo"},
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


def test_breeze_instruction_is_stored(client, admin_headers, monkeypatch):
    _ok_status(monkeypatch)
    client.put(
        "/v1/admin/tts/settings",
        json={"breeze_instruction": "  Speak slowly.  "},
        headers=admin_headers,
    )
    body = client.get("/v1/admin/tts", headers=admin_headers).json()
    assert body["breeze_instruction"] == "Speak slowly."


# ---- Filler mit Breeze -----------------------------------------------------------------


async def test_breeze_fillers_are_generated_per_voice_without_sample(client, monkeypatch):
    """Breeze braucht (anders als XTTS) kein Sample - ohne spricht es mit
    seiner eingebauten Stimme."""
    trigger = repos.create_trigger("T-breeze", "thinking", None)
    filler = repos.create_filler("Titel", "Moment bitte.", trigger["id"], True,
                                 delay_ms=0, engine="breeze")
    calls: list = []
    monkeypatch.setattr(breeze_client, "stream", _fake_engine(b"\x09\x09", calls))

    results = await filler_service.generate_audio(filler["id"])

    assert all(r["ok"] for r in results)
    assert {voice_id for _, voice_id in calls} == {v["id"] for v in repos.list_voices()}
    for voice in repos.list_voices():
        assert filler_service.has_audio(filler["id"], voice["id"])


async def test_breeze_filler_runs_through_the_shared_preparation(client, monkeypatch):
    """Breeze-Filler bekommen dieselbe Aufbereitung wie XTTS (v1.16): sauberes
    Satzende, getrimmte Stille - und mit voice_id nur diese eine Stimme."""
    trigger = repos.create_trigger("T-breeze-2", "thinking", None)
    filler = repos.create_filler("Titel", "Moment bitte...", trigger["id"], True,
                                 delay_ms=0, engine="breeze")
    silence = b"\x00\x00" * 4800  # 0,2 s
    tone = b"".join(int(8000 * math.sin(i / 8)).to_bytes(2, "little", signed=True)
                    for i in range(24000))  # 1 s, plausibel fuer 13 Zeichen
    calls: list = []

    async def fake_breeze(text, voice_id=None, language=None):
        calls.append((text, voice_id))
        yield 24000, silence + tone + silence

    monkeypatch.setattr(breeze_client, "stream", fake_breeze)

    results = await filler_service.generate_audio(filler["id"], "default-de-male")

    assert calls == [("Moment bitte.", "default-de-male")]
    assert [r["voice_id"] for r in results] == ["default-de-male"]
    assert results[0]["ok"] is True and "warning" not in results[0]
    assert not filler_service.has_audio(filler["id"], "default-de-female")
    pcm, rate = filler_service.load_audio(filler_service.audio_path(filler["id"], "default-de-male"))
    assert rate == 24000
    assert len(pcm) < len(silence + tone + silence)  # Stille vorn/hinten gekappt


def test_filler_can_be_created_with_breeze_engine(client, admin_headers):
    trigger = client.get("/v1/admin/triggers", headers=admin_headers).json()[0]
    response = client.post(
        "/v1/admin/fillers",
        json={"title": "x", "text": "y", "trigger_id": trigger["id"], "engine": "breeze"},
        headers=admin_headers,
    )
    assert response.status_code == 201
    assert response.json()["engine"] == "breeze"


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
