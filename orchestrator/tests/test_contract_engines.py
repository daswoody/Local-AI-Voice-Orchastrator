"""Engines nach dem Engine-Vertrag (v1.20): Client, Registry aus der
Datenbank, Status, Aktivieren mit Entladen auf derselben Karte."""

import io
import math
import wave
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from orchestrator import repos
from orchestrator.config import settings
from orchestrator.services import filler_service, gpu_manager, tts_engines
from orchestrator.services.tts_client import ContractClient, ContractEngineError, xtts_client


def _mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_with_transport(*args, **kwargs):
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_transport)


def _write_sample(voice_id: str, width: int = 3, channels: int = 2, rate: int = 48000) -> None:
    path = Path(settings.voices_dir) / f"{voice_id}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    full = (1 << (8 * width - 1)) - 1
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(b"".join(
            int(0.5 * full * math.sin(i / 9)).to_bytes(width, "little", signed=True) * channels
            for i in range(rate // 2)))


def _form(request: httpx.Request) -> dict[str, bytes]:
    """Formularfelder eines Requests -> {name: bytes}. Ohne Datei schickt
    httpx URL-kodiert statt multipart - die Engine nimmt beides."""
    if request.headers["content-type"].startswith("application/x-www-form-urlencoded"):
        from urllib.parse import parse_qsl

        return {key: value.encode() for key, value in parse_qsl(request.content.decode())}
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    fields = {}
    for part in request.content.split(b"--" + boundary):
        head, _, body = part.partition(b"\r\n\r\n")
        if b'name="' in head:
            name = head.split(b'name="')[1].split(b'"')[0].decode()
            fields[name] = body.removesuffix(b"\r\n")
    return fields


def _pcm_answer(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"x-sample-rate": "24000"}, content=b"\x07\x07" * 2400)


qwen3 = ContractClient(lambda: "http://tts-qwen3:8000")


# ---- Client --------------------------------------------------------------------------------


async def test_client_sends_text_language_and_the_normalized_sample(monkeypatch):
    _write_sample("default-de-female")
    repos.update_voice("default-de-female", {"sample_text": "Das ist mein Sample."})
    requests: list[httpx.Request] = []
    _mock_http(monkeypatch, lambda request: requests.append(request) or _pcm_answer(request))

    chunks = [c async for c in qwen3.stream("Hallo Welt.", "default-de-female", load_timeout_s=240)]

    assert chunks == [(24000, b"\x07\x07" * 2400)]
    request = requests[0]
    assert str(request.url) == "http://tts-qwen3:8000/v1/synthesize"
    form = _form(request)
    assert form["text"] == "Hallo Welt.".encode() and form["language"] == b"de"
    assert form["voice_id"] == b"default-de-female" and form["load_timeout_s"] == b"240"
    assert form["ref_text"] == "Das ist mein Sample.".encode()
    with wave.open(io.BytesIO(form["ref_audio"]), "rb") as reference:
        assert (reference.getnchannels(), reference.getsampwidth(), reference.getframerate()) == (1, 2, 24000)


async def test_sample_without_transcript_is_sent_alone_and_no_sample_sends_none(monkeypatch):
    _write_sample("default-de-female")
    requests: list[httpx.Request] = []
    _mock_http(monkeypatch, lambda request: requests.append(request) or _pcm_answer(request))

    [c async for c in qwen3.stream("Hallo.", "default-de-female")]
    [c async for c in qwen3.stream("Hallo.", "default-de-male")]

    with_sample, without_sample = (_form(r) for r in requests)
    assert "ref_audio" in with_sample and "ref_text" not in with_sample
    assert with_sample["load_timeout_s"] == b"0.0"  # Live-Turn: nicht aufs Laden warten
    assert "ref_audio" not in without_sample


async def test_engine_errors_carry_its_reason(monkeypatch):
    _mock_http(monkeypatch, lambda request: httpx.Response(
        503, json={"detail": "Modell wird geladen - gleich noch einmal versuchen"}))

    with pytest.raises(ContractEngineError, match="Modell wird geladen") as error:
        [c async for c in qwen3.stream("Hallo.", "default-de-female")]
    assert error.value.status_code == 503


async def test_busy_engine_is_asked_again(monkeypatch):
    monkeypatch.setattr(qwen3, "busy_wait_s", 0)
    answers = iter([httpx.Response(409, json={"detail": "Engine ist belegt"}),
                    _pcm_answer(None)])
    _mock_http(monkeypatch, lambda request: next(answers))

    assert [c async for c in qwen3.stream("Hallo.", "default-de-female")] == [(24000, b"\x07\x07" * 2400)]


# ---- Registry und Verwaltung im Panel ------------------------------------------------------


def test_engines_can_be_added_changed_and_removed(client, admin_headers, monkeypatch):
    monkeypatch.setattr(tts_engines, "engine_status", AsyncMock(return_value={"status": "idle", "detail": ""}))

    added = client.post("/v1/admin/tts/engines", headers=admin_headers,
                        json={"id": "kokoro", "url": "kokoro:8000 ", "label": "Kokoro"})

    assert added.status_code == 201 and added.json()["url"] == "http://kokoro:8000"
    assert "kokoro" in tts_engines.engine_ids()
    assert tts_engines.get_engine("kokoro")["gpu_service"] == "tts-kokoro"
    assert "tts-kokoro" in gpu_manager.services()
    assert client.post("/v1/admin/tts/engines", headers=admin_headers,
                       json={"id": "kokoro", "url": "http://192.168.2.105:8001"}).status_code == 201
    assert tts_engines.get_engine("kokoro")["url"] == "http://192.168.2.105:8001"

    assert client.delete("/v1/admin/tts/engines/kokoro", headers=admin_headers).status_code == 204
    assert "kokoro" not in tts_engines.engine_ids()


def test_invalid_engine_entries_are_refused(client, admin_headers):
    def add(**payload):
        return client.post("/v1/admin/tts/engines", headers=admin_headers, json=payload).status_code

    assert add(id="xtts", url="http://x:1") == 400        # eingebaute Engine
    assert add(id="Mit Leerzeichen", url="http://x:1") == 422
    assert add(id="neu", url="ftp://x") == 400
    assert add(id="neu", url="   ") == 400


def test_the_active_engine_cannot_be_removed(client, admin_headers):
    repos.set_setting(tts_engines.ACTIVE_ENGINE_SETTING, "qwen3")
    assert client.delete("/v1/admin/tts/engines/qwen3", headers=admin_headers).status_code == 409


@pytest.mark.parametrize(("device", "expected"), [
    ({"state": "ready", "effective": "cuda:1"}, {"status": "ok", "detail": "geladen auf cuda:1"}),
    ({"state": "idle"}, {"status": "idle"}),
    ({"state": "loading"}, {"status": "loading"}),
    ({"state": "preparing"}, {"status": "loading"}),
    ({"state": "error", "detail": "Laden fehlgeschlagen: CUDA out of memory"},
     {"status": "error", "detail": "Laden fehlgeschlagen: CUDA out of memory"}),
])
async def test_status_follows_the_engine_state(monkeypatch, device, expected):
    _mock_http(monkeypatch, lambda request: httpx.Response(200, json=device))
    status = await tts_engines.engine_status("qwen3")
    assert {key: status[key] for key in expected} == expected


async def test_overview_reads_the_engine_profile(client, admin_headers, monkeypatch):
    def handler(request):
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={"name": "Qwen3-TTS 1.7B", "languages": ["de", "en"],
                                             "needs_sample": True, "description": "Klont."})
        if request.url.path == "/v1/device":
            return httpx.Response(200, json={"state": "idle"})
        raise httpx.ConnectError("nicht erreichbar")

    _mock_http(monkeypatch, handler)

    engines = {e["id"]: e for e in await tts_engines.overview()}

    assert engines["qwen3"]["label"] == "Qwen3-TTS"  # Name aus dem Panel geht vor
    assert "Sprachen: de, en." in engines["qwen3"]["description"]
    assert engines["qwen3"]["status"]["status"] == "idle"


# ---- Aktivieren: laden, Nachbarn auf derselben Karte entladen -------------------------------


def _fake_services(monkeypatch, live: dict[str, dict]) -> list:
    """live: Dienst -> Status; set_service_device aendert ihn wie der echte Dienst."""
    calls = []

    async def status(name):
        return {"reachable": True, **live.get(name, {"assigned": "cuda:0", "loaded": False})}

    async def set_device(name, device, compute_type=None):
        calls.append((name, device))
        loaded = device != gpu_manager.OFF
        live[name] = {"assigned": device, "effective": device if loaded else None,
                      "loaded": loaded, "state": "ready" if loaded else "off"}
        return live[name]

    monkeypatch.setattr(gpu_manager, "service_status", status)
    monkeypatch.setattr(gpu_manager, "set_service_device", set_device)
    return calls


async def test_activating_unloads_the_neighbour_on_the_same_card(monkeypatch):
    live = {"tts-xtts": {"assigned": "cuda:0", "effective": "cuda:0", "loaded": True},
            "tts-qwen3": {"assigned": "cuda:0", "effective": None, "loaded": False, "state": "idle"}}
    calls = _fake_services(monkeypatch, live)

    notes = await tts_engines.activate("qwen3")

    assert calls == [("tts-xtts", "off"), ("tts-qwen3", "cuda:0")]
    assert tts_engines.active_engine() == "qwen3"
    assert gpu_manager.is_off("tts-xtts") and gpu_manager.last_device("tts-xtts") == "cuda:0"
    assert notes == ["XTTS entladen (gleiche Karte, cuda:0).", "Qwen3-TTS geladen auf cuda:0."]
    # XTTS ist aus -> Piper faengt Ausfaelle ab
    assert tts_engines.fallback_engine() == "piper"

    # Zurueck zu XTTS: wieder an auf seiner alten Karte, Qwen3 wird entladen.
    calls.clear()
    await tts_engines.activate("xtts")
    assert calls == [("tts-qwen3", "off"), ("tts-xtts", "cuda:0")]
    assert tts_engines.fallback_engine() == "xtts"


async def test_engines_on_the_other_card_stay_loaded(monkeypatch):
    gpu_manager.store_assignment("tts-qwen3", "cuda:1")
    live = {"tts-xtts": {"assigned": "cuda:0", "effective": "cuda:0", "loaded": True}}
    calls = _fake_services(monkeypatch, live)

    await tts_engines.activate("qwen3")

    assert calls == [("tts-qwen3", "cuda:1")]
    assert not gpu_manager.is_off("tts-xtts") and tts_engines.fallback_engine() == "xtts"


async def test_load_failure_is_reported_but_the_choice_stays(monkeypatch):
    live = {"tts-xtts": {"assigned": "cuda:1", "effective": "cuda:1", "loaded": True}}
    _fake_services(monkeypatch, live)

    async def failing(name, device, compute_type=None):
        return {"assigned": device, "effective": None, "loaded": False, "state": "error",
                "detail": "Laden fehlgeschlagen: CUDA out of memory"}

    monkeypatch.setattr(gpu_manager, "set_service_device", failing)

    notes = await tts_engines.activate("qwen3")

    assert notes == ["Qwen3-TTS: Laden fehlgeschlagen: CUDA out of memory"]
    assert tts_engines.active_engine() == "qwen3"


# ---- Sprechen: Live-Turn, Probehoeren, Filler ------------------------------------------------


async def test_live_answer_falls_back_while_the_engine_loads(monkeypatch):
    repos.set_setting(tts_engines.ACTIVE_ENGINE_SETTING, "qwen3")
    _mock_http(monkeypatch, lambda request: httpx.Response(503, json={"detail": "Modell wird geladen"}))

    async def xtts(text, voice_id=None, language=None):
        yield 24000, b"\x0a\x0a" * 10

    monkeypatch.setattr(xtts_client, "stream", xtts)

    spoken = [chunk async for chunk in tts_engines.stream_main("Hallo.", "default-de-female")]

    assert spoken == [(24000, b"\x0a\x0a" * 10)]


def test_preview_waits_for_the_engine_to_load(client, admin_headers, monkeypatch):
    requests: list[httpx.Request] = []
    _mock_http(monkeypatch, lambda request: requests.append(request) or _pcm_answer(request))

    response = client.post("/v1/admin/tts/preview", headers=admin_headers,
                           json={"engine": "qwen3", "voice_id": "default-de-female", "text": "Hallo"})

    assert response.status_code == 200
    assert _form(requests[0])["load_timeout_s"] == str(tts_engines.PREVIEW_LOAD_TIMEOUT_S).encode()


async def test_fillers_can_be_spoken_by_a_contract_engine(client, monkeypatch):
    for voice in ("default-de-female", "default-de-male"):
        _write_sample(voice, width=2, channels=1, rate=24000)
    tone = b"".join(int(8000 * math.sin(i / 8)).to_bytes(2, "little", signed=True) for i in range(24000))
    _mock_http(monkeypatch, lambda request: httpx.Response(
        200, headers={"x-sample-rate": "24000"}, content=tone))
    trigger = repos.create_trigger("T-qwen3", "thinking", None)
    filler = repos.create_filler("Titel", "Moment bitte.", trigger["id"], True, delay_ms=0, engine="qwen3")

    results = await filler_service.generate_audio(filler["id"])

    assert results and all(result["ok"] for result in results)
    assert filler_service.has_audio(filler["id"], "default-de-male")
