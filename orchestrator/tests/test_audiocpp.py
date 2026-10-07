"""audio.cpp als Anbieter fuer Sprachausgabe (v1.21): Modell-Liste wie bei
LiteLLM, Client (Klonen, Sprache, abgelehnte Felder), Status, Aktivieren mit
Entladen und XTTS als Rueckfallebene."""

import asyncio
import audioop
import base64
import io
import json
import math
import socket
import wave
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from orchestrator import repos
from orchestrator.audio import Pcm16StreamConverter
from orchestrator.config import settings
from orchestrator.services import audiocpp, filler_service, gpu_manager, tts_engines
from orchestrator.services.sentences import split_sentences
from orchestrator.services.tts_client import piper_client, xtts_client

URL = "http://audiocpp:8080"


def _tone(seconds: float, rate: int) -> bytes:
    return b"".join(int(8000 * math.sin(i / 8)).to_bytes(2, "little", signed=True)
                    for i in range(int(rate * seconds)))


def _wav(seconds: float = 0.5, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(_tone(seconds, rate))
    return buffer.getvalue()


def _write_sample(voice_id: str) -> None:
    path = Path(settings.voices_dir) / f"{voice_id}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav(1.0, rate=48000))


class FakeAudioCpp:
    """Nachbau der audio.cpp-Server-API (app/server/runtime.cpp) fuer
    httpx.MockTransport: gleiche Routen, gleiche Fehlerform."""

    def __init__(self) -> None:
        self.models = {
            "qwen3-tts": {"family": "qwen3_tts", "task": "tts", "mode": "offline", "loaded": False},
            "kokoro": {"family": "kokoro_tts", "task": "tts", "mode": "offline", "loaded": True,
                       "voices": ["af_heart", "ff_siwis"]},
            "qwen3-asr": {"family": "qwen3_asr", "task": "asr", "mode": "offline", "loaded": False},
            # Streamt (server.json "mode": "streaming"); kennt kein reference_text.
            "pocket-tts": {"family": "pocket_tts", "task": "tts", "mode": "streaming", "loaded": False},
            "voxcpm2": {"family": "voxcpm2", "task": "tts", "mode": "streaming", "loaded": False,
                        "rate": 48000},
        }
        self.speech: list[dict] = []
        self.unload_calls: list[list[str]] = []
        self.rate = 24000
        # Stream: Laenge eines speech.audio.delta; gate haelt den Stream nach
        # dem ersten Delta an (zeigt, dass der Client nicht aufs Ende wartet).
        self.delta_s = 0.25
        self.deltas = 3
        self.gate: asyncio.Event | None = None
        self.fail_mid_stream: str | None = None
        # Wie audio.cpp: Scheitert das Hochladen der Decoder-Konstanten (VRAM),
        # bleibt das Modell kaputt, bis es entladen wird.
        self.no_vram: set[str] = set()
        self.broken: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "backend": "cuda", "models": len(self.models),
                                             "ui": True, "ui_management": False})
        if path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": model_id, "object": "model", "family": m["family"], "task": m["task"],
                 "mode": m["mode"], "loaded": m["loaded"], "path": f"/models/{model_id}"}
                for model_id, m in self.models.items()]})
        if path == "/v1/audio/voices":
            model = self.models.get(request.url.params.get("model"), {})
            return httpx.Response(200, json={"voices": model.get("voices", [])})
        if path == "/v1/tasks/unload_models":
            ids = json.loads(request.content)["model_ids"]
            self.unload_calls.append(ids)
            unloaded = [i for i in ids if self.models.get(i, {}).get("loaded")]
            for model_id in unloaded:
                self.models[model_id]["loaded"] = False
                self.broken.discard(model_id)
            return httpx.Response(200, json={"unloaded": unloaded, "not_found": []})
        if path == "/v1/audio/speech":
            payload = json.loads(request.content)
            self.speech.append(payload)
            model = self.models.get(payload["model"])
            if model is None:
                return self.error(500, f"unknown model id: {payload['model']}")
            if model["mode"] == "streaming" and model["family"] in ("qwen3_tts", "kokoro_tts"):
                # Laden scheitert: Die Familie kann nur offline.
                return self.error(500, f"configured model does not provide streaming execution: {payload['model']}")
            streamed = "stream_format" in payload or payload.get("stream")
            if streamed and model["mode"] != "streaming":
                return self.error(500, "speech streaming requires a model configured with mode=streaming")
            if streamed and payload.get("response_format") != "pcm":
                return self.error(500, "streaming speech currently supports response_format=pcm")
            model["loaded"] = True
            # Fehler beim Rechnen: als WAV-Request HTTP 500, im Stream (Header
            # sind dann schon raus) ein error-Event - wie in audio.cpp.
            failure = self._failure(payload, model, streamed)
            rate = model.get("rate", self.rate)
            if streamed:
                return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"},
                                      content=self._events(failure, rate))
            if failure:
                return self.error(500, failure)
            return httpx.Response(200, content=_wav(0.25, rate), headers={"content-type": "audio/wav"})
        return self.error(404, f"unknown endpoint: {path}", "not_found")

    def _failure(self, payload: dict, model: dict, streamed: bool) -> str | None:
        prefix = "qwen3_tts.speech_tokenizer_decoder.constants"
        if payload["model"] in self.broken:
            return f"{prefix} constant tensor cache graph used a different tensor sequence"
        if payload["model"] in self.no_vram:
            self.broken.add(payload["model"])
            return f"failed to allocate {prefix} constant tensor cache"
        if model["family"] == "qwen3_tts" and payload.get("language") not in (None, "german", "english"):
            return f"Qwen3 talker unsupported language: {payload['language']}"
        if model["family"] == "kokoro_tts" and "reference_text" in payload:
            return "unknown Kokoro request option: reference_text"
        if model["family"] == "pocket_tts" and "reference_text" in payload:
            return "unknown PocketTTS request option: reference_text"
        if streamed and model["family"] == "voxcpm2" and payload.get("options", {}).get("retry_badcase") is not False:
            return "VoxCPM2 streaming generation requires retry_badcase=false"
        return None

    async def _events(self, failure: str | None, rate: int):
        def event(data: dict) -> bytes:
            return f"data: {json.dumps(data)}\n\n".encode()

        if failure:
            yield event({"type": "error", "error": {"message": failure}})
            return
        pcm = _tone(self.delta_s * self.deltas, rate)
        size = len(pcm) // self.deltas // 2 * 2
        for index in range(self.deltas):
            piece = pcm[index * size:] if index == self.deltas - 1 else pcm[index * size:(index + 1) * size]
            yield event({"type": "speech.audio.delta", "audio": base64.b64encode(piece).decode()})
            if index == 0 and self.gate is not None:
                await self.gate.wait()
        if self.fail_mid_stream:
            yield event({"type": "error", "error": {"message": self.fail_mid_stream}})
            return
        yield event({"type": "speech.audio.done", "timing": {"ttft_ms": 12.5}})
        yield b"data: [DONE]\n\n"

    @staticmethod
    def error(status: int, message: str, kind: str = "server_error") -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": message, "type": kind}})


@pytest.fixture
def server(monkeypatch) -> FakeAudioCpp:
    """audio.cpp unter URL; andere Hosts sind 'nicht erreichbar'."""
    fake = FakeAudioCpp()
    repos.set_setting(audiocpp.URL_SETTING, URL)
    _mock_http(monkeypatch, fake)
    return fake


_REAL_CLIENT = httpx.AsyncClient


def _dns_error(request: httpx.Request):
    """Wie ein Containername, den das Netzwerk nicht kennt."""
    try:
        raise socket.gaierror(-2, "Name or service not known")
    except socket.gaierror as cause:
        raise httpx.ConnectError("[Errno -2] Name or service not known", request=request) from cause


def _mock_http(monkeypatch, fake) -> None:
    """audio.cpp unter dem Host 'audiocpp', alle anderen Hosts gibt es nicht."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "audiocpp":
            return fake(request)
        _dns_error(request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *args, **kwargs: _REAL_CLIENT(*args, transport=transport, **kwargs))


def _unreachable(monkeypatch) -> None:
    _mock_http(monkeypatch, _dns_error)


def _speak_with(monkeypatch, client, tone: bytes) -> list:
    """XTTS bzw. Piper als Rueckfallebene nachstellen; merkt sich Aufrufe."""
    calls = []

    async def stream(text, voice_id=None, language=None):
        calls.append(text)
        yield 24000, tone

    monkeypatch.setattr(client, "stream", stream)
    return calls


# ---- Modell-Liste wie bei LiteLLM ---------------------------------------------------------


async def test_tts_models_become_engines_and_the_list_is_remembered(server):
    state = await tts_engines.audiocpp_server()

    assert state["reachable"] and state["health"]["backend"] == "cuda"
    ids = tts_engines.engine_ids()
    assert "audiocpp:qwen3-tts" in ids and "audiocpp:kokoro" in ids
    assert "audiocpp:qwen3-asr" not in ids  # kein TTS-Modell
    spec = tts_engines.get_engine("audiocpp:qwen3-tts")
    assert spec["label"] == "audio.cpp · qwen3-tts" and spec["family"] == "qwen3_tts"

    # Server weg: Die gemerkte Liste bleibt, die Engines zeigen "nicht erreichbar".
    server.models.clear()
    repos.set_setting(audiocpp.URL_SETTING, "http://anderswo:8080")
    engines = {e["id"]: e for e in await tts_engines.overview()}
    assert engines["audiocpp:kokoro"]["status"]["status"] == "unreachable"
    assert "Server nicht gefunden" in engines["audiocpp:kokoro"]["status"]["detail"]


async def test_without_address_there_are_no_audiocpp_engines():
    assert not any(e.startswith("audiocpp:") for e in tts_engines.engine_ids())
    state = await tts_engines.audiocpp_server()
    assert not state["configured"] and not state["reachable"]


async def test_the_active_model_stays_selectable_when_audiocpp_forgets_it(server):
    await tts_engines.audiocpp_server()
    repos.set_setting(tts_engines.ACTIVE_ENGINE_SETTING, "audiocpp:qwen3-tts")
    del server.models["qwen3-tts"]
    await tts_engines.audiocpp_server()

    assert tts_engines.active_engine() == "audiocpp:qwen3-tts"
    status = await tts_engines.engine_status("audiocpp:qwen3-tts")
    assert status["status"] == "error" and "Modell-Liste" in status["detail"]


async def test_status_follows_what_audiocpp_has_loaded(server):
    await tts_engines.audiocpp_server()

    assert (await tts_engines.engine_status("audiocpp:kokoro"))["status"] == "ok"
    assert (await tts_engines.engine_status("audiocpp:qwen3-tts"))["status"] == "idle"
    audiocpp._warm_up_errors["qwen3-tts"] = (0.0, "CUDA out of memory")
    status = await tts_engines.engine_status("audiocpp:qwen3-tts")
    assert status == {"status": "error", "detail": "Laden fehlgeschlagen: CUDA out of memory"}


async def test_a_wrong_address_is_named_as_such(monkeypatch):
    repos.set_setting(audiocpp.URL_SETTING, URL)
    transport = httpx.MockTransport(lambda request: httpx.Response(404, text="Not Found"))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *args, **kwargs: real_client(*args, transport=transport, **kwargs))

    state = await tts_engines.audiocpp_server()

    assert not state["reachable"] and "kein audio.cpp-Server" in state["detail"]


# ---- Client ----------------------------------------------------------------------------------


async def test_client_clones_sentence_by_sentence_with_sample_transcript_and_language(server):
    _write_sample("default-de-female")
    repos.update_voice("default-de-female", {"sample_text": "Das ist mein Sample."})
    await tts_engines.audiocpp_server()
    text = "Hallo, ich bin deine Heim-Assistenz. Soll ich das Licht im Wohnzimmer einschalten?"

    chunks = [c async for c in audiocpp.AudioCppClient("qwen3-tts").stream(
        text, "default-de-female", wait_for_load=True)]

    assert [p["input"] for p in server.speech] == split_sentences(text)
    assert len(server.speech) == 2 and all(rate == 24000 for rate, _ in chunks)
    first = server.speech[0]
    assert first["language"] == "german"  # Qwen3 kennt Sprachen nur beim Namen
    assert first["reference_text"] == "Das ist mein Sample."
    assert "busy_timeout_ms" not in first  # Probehoeren/Filler warten
    reference = base64.b64decode(first["voice_ref"]["data"])
    with wave.open(io.BytesIO(reference), "rb") as ref:
        assert (ref.getnchannels(), ref.getsampwidth(), ref.getframerate()) == (1, 2, 24000)


async def test_fields_a_model_rejects_are_dropped_and_remembered(server):
    _write_sample("default-de-female")
    repos.update_voice("default-de-female", {"sample_text": "Das ist mein Sample."})
    await tts_engines.audiocpp_server()
    kokoro = audiocpp.AudioCppClient("kokoro")

    assert [c async for c in kokoro.stream("Guten Morgen zusammen!", "default-de-female")]
    assert "reference_text" in server.speech[0] and "reference_text" not in server.speech[1]
    assert server.speech[1]["language"] == "de"  # ISO-Code fuer alle ausser Qwen3

    server.speech.clear()
    assert [c async for c in kokoro.stream("Noch ein Satz zum Testen.", "default-de-female")]
    assert len(server.speech) == 1 and "reference_text" not in server.speech[0]


async def test_an_unsupported_language_is_left_to_the_model(server):
    _write_sample("default-de-female")
    repos.update_voice("default-de-female", {"language": "sv"})
    await tts_engines.audiocpp_server()

    chunks = [c async for c in audiocpp.AudioCppClient("qwen3-tts").stream(
        "Hej, hur mår du idag?", "default-de-female", wait_for_load=True)]

    # "sv" hat keinen Qwen3-Namen -> gar nicht erst mitgeschickt (= Auto).
    assert chunks and "language" not in server.speech[0]


async def test_a_builtin_voice_replaces_cloning(server):
    _write_sample("default-de-female")
    audiocpp.set_voice_choice("kokoro", "ff_siwis")
    await tts_engines.audiocpp_server()

    [c async for c in audiocpp.AudioCppClient("kokoro").stream("Bonjour tout le monde!", "default-de-female")]

    assert server.speech[0]["voice"] == "ff_siwis" and "voice_ref" not in server.speech[0]
    assert "eingebauten Stimme 'ff_siwis'" in tts_engines.get_engine("audiocpp:kokoro")["description"]


async def test_live_turn_does_not_wait_for_loading(server, monkeypatch):
    started = []
    monkeypatch.setattr(audiocpp, "start_warm_up", lambda model, voice_id, force=False: started.append(model))

    with pytest.raises(audiocpp.AudioCppNotReady):
        [c async for c in audiocpp.AudioCppClient("qwen3-tts").stream("Hallo zusammen.", "default-de-female")]

    assert started == ["qwen3-tts"] and server.speech == []


async def test_live_turn_bounds_only_the_wait_for_the_first_sentence(server):
    _write_sample("default-de-female")
    text = "Das ist der erste Satz der Antwort. Und hier kommt noch der zweite Satz."

    [c async for c in audiocpp.AudioCppClient("kokoro").stream(text, "default-de-female")]

    assert len(server.speech) == 2
    assert server.speech[0]["busy_timeout_ms"] == audiocpp.LIVE_BUSY_TIMEOUT_MS
    assert "busy_timeout_ms" not in server.speech[1]


async def test_audiocpp_errors_are_translated(server):
    with pytest.raises(audiocpp.AudioCppError, match="nicht \\(mehr\\) eingetragen"):
        [c async for c in audiocpp.AudioCppClient("gibtsnicht").stream("Hallo zusammen.", "x",
                                                                        wait_for_load=True)]


async def test_a_broken_model_is_unloaded_and_tried_fresh(server):
    """Nutzer-Report: "... constant tensor cache graph used a different tensor
    sequence (HTTP 500)" beim Probehoeren - Folgefehler eines gescheiterten
    ersten Satzes, audio.cpp heilt erst durch Entladen."""
    server.models["qwen3-tts"]["loaded"] = True
    server.broken.add("qwen3-tts")
    await tts_engines.audiocpp_server()

    chunks = [c async for c in audiocpp.AudioCppClient("qwen3-tts").stream(
        "Hallo, wie geht es dir heute?", "default-de-female", wait_for_load=True)]

    assert chunks and server.unload_calls == [["qwen3-tts"]] and len(server.speech) == 2


async def test_live_turn_unloads_a_broken_model_but_does_not_wait(server):
    server.models["qwen3-tts"]["loaded"] = True
    server.broken.add("qwen3-tts")

    with pytest.raises(audiocpp.AudioCppError, match="kaputten Zustand") as error:
        [c async for c in audiocpp.AudioCppClient("qwen3-tts").stream("Hallo, wie geht es dir?", "x")]

    assert server.unload_calls == [["qwen3-tts"]] and len(server.speech) == 1
    assert not tts_engines.engine_gone(error.value)  # kein Ausfall des Servers


async def test_too_little_vram_is_explained_and_the_model_left_clean(server):
    server.no_vram.add("qwen3-tts")

    with pytest.raises(audiocpp.AudioCppError) as error:
        [c async for c in audiocpp.AudioCppClient("qwen3-tts").stream(
            "Hallo, wie geht es dir heute?", "default-de-female", wait_for_load=True)]

    assert "failed to allocate" in str(error.value) and "zu wenig VRAM" in str(error.value)
    # Entladen, damit der naechste Versuch nicht am halb gefuellten Cache scheitert.
    assert server.unload_calls == [["qwen3-tts"]] and "qwen3-tts" not in server.broken


async def test_activation_tests_and_heals_an_already_loaded_model(server, monkeypatch):
    _write_sample("default-de-female")
    server.models["kokoro"]["loaded"] = False
    server.models["qwen3-tts"]["loaded"] = True
    server.broken.add("qwen3-tts")
    _fake_gpu_services(monkeypatch, {"tts-xtts": {"assigned": "cuda:1", "effective": "cuda:1", "loaded": True}})
    await tts_engines.audiocpp_server()

    notes = await tts_engines.activate("audiocpp:qwen3-tts")

    assert notes == ["audio.cpp/qwen3-tts ist geladen."]
    assert server.unload_calls == [["qwen3-tts"]] and "qwen3-tts" not in server.broken


# ---- Streaming (v1.23) -----------------------------------------------------------------------


async def _learn(model: str) -> None:
    """Ein normaler Request, wie beim Laden nach dem Aktivieren: Daraus lernt
    der Client die Samplerate, die audio.cpp im Stream nicht nennt."""
    [c async for c in audiocpp.AudioCppClient(model).stream("Hallo, ich bin bereit.", "", wait_for_load=True)]


async def test_a_streaming_model_learns_its_rate_once_then_streams(server):
    server.models["pocket-tts"]["loaded"] = True
    text = "Das ist der erste Satz der Antwort. Und hier kommt noch der zweite Satz."

    chunks = [c async for c in audiocpp.AudioCppClient("pocket-tts").stream(text, "default-de-female")]

    # Satz 1 als WAV (Samplerate lernen), ab Satz 2 als Stream.
    assert "stream_format" not in server.speech[0]
    assert server.speech[1]["stream_format"] == "sse" and server.speech[1]["response_format"] == "pcm"
    assert chunks and all(rate == 24000 for rate, _ in chunks)

    server.speech.clear()
    [c async for c in audiocpp.AudioCppClient("pocket-tts").stream("Noch eine Antwort.", "default-de-female")]
    assert server.speech[0]["stream_format"] == "sse"  # gemerkt, auch fuer den ersten Satz


async def test_streamed_audio_is_passed_on_before_the_sentence_is_finished(server):
    server.models["pocket-tts"]["loaded"] = True
    await _learn("pocket-tts")
    server.gate = asyncio.Event()

    stream = audiocpp.AudioCppClient("pocket-tts").stream("Hallo, schoen dass du da bist.", "default-de-female")
    rate, first = await asyncio.wait_for(stream.__anext__(), timeout=2)

    # audio.cpp rechnet noch (gate zu) - der erste Ton ist trotzdem schon da.
    assert rate == 24000 and len(first) >= int(24000 * audiocpp.STREAM_CHUNK_S) * 2
    server.gate.set()
    rest = [c async for c in stream]
    assert len(first) + sum(len(pcm) for _, pcm in rest) == len(_tone(server.delta_s * server.deltas, 24000))


async def test_offline_models_keep_sending_whole_sentences(server):
    server.models["kokoro"]["loaded"] = True
    text = "Das ist der erste Satz der Antwort. Und hier kommt noch der zweite Satz."

    for _ in range(2):
        [c async for c in audiocpp.AudioCppClient("kokoro").stream(text, "default-de-female")]

    assert len(server.speech) == 4 and not any("stream_format" in p for p in server.speech)


async def test_voxcpm2_streams_at_its_own_rate_without_bad_case_retries(server):
    server.models["voxcpm2"]["loaded"] = True
    await _learn("voxcpm2")

    chunks = [c async for c in audiocpp.AudioCppClient("voxcpm2").stream(
        "Hallo, schoen dass du da bist.", "default-de-female")]

    # Ohne die Option lehnt audio.cpp den Stream ab ("requires retry_badcase=false").
    assert server.speech[-1]["options"] == {"retry_badcase": False}
    assert {rate for rate, _ in chunks} == {48000}  # umgerechnet wird erst im Router, am Stueck
    assert b"".join(pcm for _, pcm in chunks) == _tone(server.delta_s * server.deltas, 48000)


async def test_a_different_model_behind_the_same_id_is_learned_again(server):
    server.models["pocket-tts"]["loaded"] = True
    await _learn("pocket-tts")
    server.speech.clear()
    # In der server.json steckt jetzt ein anderes Modell hinter der ID.
    server.models["pocket-tts"].update(family="voxcpm2", rate=48000)

    chunks = [c async for c in audiocpp.AudioCppClient("pocket-tts").stream(
        "Hallo, schoen dass du da bist.", "default-de-female")]

    assert "stream_format" not in server.speech[0] and {rate for rate, _ in chunks} == {48000}


async def test_a_field_rejected_inside_the_stream_is_dropped_before_any_audio(server):
    _write_sample("default-de-female")
    repos.update_voice("default-de-female", {"sample_text": "Das ist mein Sample."})
    server.models["pocket-tts"]["loaded"] = True
    await _learn("pocket-tts")
    server.speech.clear()

    chunks = [c async for c in audiocpp.AudioCppClient("pocket-tts").stream(
        "Guten Morgen zusammen!", "default-de-female")]

    first, second = server.speech
    assert first["stream_format"] == second["stream_format"] == "sse"
    assert "reference_text" in first and "reference_text" not in second and "voice_ref" in second
    assert chunks


async def test_an_error_in_the_middle_of_a_stream_is_not_retried(server):
    server.models["pocket-tts"]["loaded"] = True
    await _learn("pocket-tts")
    server.speech.clear()
    server.fail_mid_stream = "CUDA error: an illegal memory access was encountered"

    received = []
    with pytest.raises(audiocpp.AudioCppError, match="illegal memory access"):
        async for chunk in audiocpp.AudioCppClient("pocket-tts").stream(
                "Hallo, schoen dass du da bist.", "default-de-female"):
            received.append(chunk)

    # Gespieltes Audio laesst sich nicht zuruecknehmen: kein zweiter Versuch.
    assert received and len(server.speech) == 1


async def test_a_broken_streaming_model_is_unloaded_and_tried_fresh(server):
    server.models["pocket-tts"]["loaded"] = True
    await _learn("pocket-tts")
    server.speech.clear()
    server.broken.add("pocket-tts")

    chunks = [c async for c in audiocpp.AudioCppClient("pocket-tts").stream(
        "Hallo, wie geht es dir heute?", "x", wait_for_load=True)]

    assert chunks and server.unload_calls == [["pocket-tts"]]
    assert len(server.speech) == 2 and all(p["stream_format"] == "sse" for p in server.speech)


async def test_the_live_answer_streams_from_a_streaming_model(server):
    server.models["pocket-tts"]["loaded"] = True
    await tts_engines.audiocpp_server()
    repos.set_setting(tts_engines.ACTIVE_ENGINE_SETTING, "audiocpp:pocket-tts")
    await _learn("pocket-tts")
    server.speech.clear()
    server.gate = asyncio.Event()

    answer = tts_engines.stream_main("Hallo, schoen dass du da bist.", "default-de-female")
    rate, first = await asyncio.wait_for(answer.__anext__(), timeout=2)
    server.gate.set()
    rest = [c async for c in answer]

    assert rate == 24000 and first and rest
    assert server.speech[0]["busy_timeout_ms"] == audiocpp.LIVE_BUSY_TIMEOUT_MS


async def test_streaming_mode_on_an_offline_only_family_is_explained(server):
    # Qwen3-TTS kann in audio.cpp nur offline - "mode": "streaming" laedt nicht.
    server.models["qwen3-tts"]["mode"] = "streaming"

    with pytest.raises(audiocpp.AudioCppError, match="kann in audio.cpp nicht streamen"):
        [c async for c in audiocpp.AudioCppClient("qwen3-tts").stream("Hallo zusammen.", "x", wait_for_load=True)]


def test_the_stream_converter_matches_a_one_shot_conversion():
    stereo = b"".join(int(8000 * math.sin(i / 8)).to_bytes(2, "little", signed=True) * 2 for i in range(4800))
    converter = Pcm16StreamConverter(48000, 24000, channels=2)

    # Stuecke mitten im Sample zerschnitten, wie sie ueber das Netz kommen koennen.
    out = b"".join(converter.convert(stereo[i:i + 1001]) for i in range(0, len(stereo), 1001))

    mono = audioop.tomono(stereo, 2, 0.5, 0.5)
    assert out == audioop.ratecv(mono, 2, 1, 48000, 24000, None)[0]


# ---- Hauptstimme: Fallback auf XTTS ---------------------------------------------------------


async def test_live_answer_falls_back_to_xtts_while_the_model_loads(server, monkeypatch):
    await tts_engines.audiocpp_server()
    repos.set_setting(tts_engines.ACTIVE_ENGINE_SETTING, "audiocpp:qwen3-tts")
    monkeypatch.setattr(audiocpp, "start_warm_up", lambda *args, **kwargs: None)
    xtts_calls = _speak_with(monkeypatch, xtts_client, b"\x0a\x0a" * 10)

    spoken = [c async for c in tts_engines.stream_main("Hallo.", "default-de-female")]

    assert spoken == [(24000, b"\x0a\x0a" * 10)] and xtts_calls == ["Hallo."]


async def test_unreachable_audiocpp_brings_back_the_xtts_it_displaced(monkeypatch):
    repos.set_setting(audiocpp.URL_SETTING, URL)
    repos.set_setting(tts_engines.ACTIVE_ENGINE_SETTING, "audiocpp:qwen3-tts")
    gpu_manager.store_assignment("tts-xtts", "cuda:1")
    gpu_manager.store_assignment("tts-xtts", gpu_manager.OFF)
    gpu_manager.mark_auto_off("tts-xtts", "audiocpp:qwen3-tts")
    _unreachable(monkeypatch)
    apply_device = AsyncMock(return_value={"loaded": True})
    monkeypatch.setattr(gpu_manager, "apply_device", apply_device)
    piper_calls = _speak_with(monkeypatch, piper_client, b"\x05\x05")

    spoken = [c async for c in tts_engines.stream_main("Hallo.", "default-de-female")]
    await tts_engines._revival

    assert spoken == [(24000, b"\x05\x05")] and piper_calls == ["Hallo."]  # XTTS ist noch aus
    apply_device.assert_awaited_once_with("tts-xtts", "cuda:1")


async def test_a_failed_revival_is_not_retried_every_turn(monkeypatch):
    repos.set_setting(audiocpp.URL_SETTING, URL)
    repos.set_setting(tts_engines.ACTIVE_ENGINE_SETTING, "audiocpp:qwen3-tts")
    gpu_manager.store_assignment("tts-xtts", gpu_manager.OFF)
    gpu_manager.mark_auto_off("tts-xtts", "audiocpp:qwen3-tts")
    _unreachable(monkeypatch)
    apply_device = AsyncMock(side_effect=RuntimeError("XTTS auch weg"))
    monkeypatch.setattr(gpu_manager, "apply_device", apply_device)
    _speak_with(monkeypatch, piper_client, b"\x05\x05")

    [c async for c in tts_engines.stream_main("Hallo.", "default-de-female")]
    await tts_engines._revival
    [c async for c in tts_engines.stream_main("Hallo.", "default-de-female")]
    await tts_engines._revival

    assert apply_device.await_count == 1


async def test_xtts_switched_off_by_hand_stays_off(monkeypatch):
    repos.set_setting(audiocpp.URL_SETTING, URL)
    repos.set_setting(tts_engines.ACTIVE_ENGINE_SETTING, "audiocpp:qwen3-tts")
    gpu_manager.store_assignment("tts-xtts", gpu_manager.OFF)
    _unreachable(monkeypatch)
    apply_device = AsyncMock()
    monkeypatch.setattr(gpu_manager, "apply_device", apply_device)
    _speak_with(monkeypatch, piper_client, b"\x05\x05")

    [c async for c in tts_engines.stream_main("Hallo.", "default-de-female")]

    assert tts_engines._revival is None and not apply_device.called


# ---- Aktivieren ------------------------------------------------------------------------------


def _fake_gpu_services(monkeypatch, live: dict[str, dict]) -> list:
    calls = []

    async def status(name):
        return {"reachable": True, **live.get(name, {"assigned": "cuda:0", "loaded": False})}

    async def set_device(name, device, compute_type=None):
        calls.append((name, device))
        loaded = device != gpu_manager.OFF
        live[name] = {"assigned": device, "effective": device if loaded else None, "loaded": loaded}
        return live[name]

    monkeypatch.setattr(gpu_manager, "service_status", status)
    monkeypatch.setattr(gpu_manager, "set_service_device", set_device)
    return calls


async def test_activating_a_model_unloads_other_audiocpp_voices_and_loads_it(server, monkeypatch):
    _write_sample("default-de-female")
    live = {"tts-xtts": {"assigned": "cuda:0", "effective": "cuda:0", "loaded": True}}
    calls = _fake_gpu_services(monkeypatch, live)
    await tts_engines.audiocpp_server()

    notes = await tts_engines.activate("audiocpp:qwen3-tts")

    assert server.unload_calls == [["kokoro"]]
    assert server.models["qwen3-tts"]["loaded"] and server.speech[0]["input"] == audiocpp.WARM_UP_TEXT
    assert notes == ["audio.cpp/kokoro entladen (Platz fuer die neue Hauptstimme).",
                     "audio.cpp/qwen3-tts geladen."]
    # Karte nicht angegeben: XTTS bleibt als Rueckfallebene geladen.
    assert calls == [] and tts_engines.fallback_engine() == "xtts"
    assert tts_engines.active_engine() == "audiocpp:qwen3-tts"


async def test_on_a_shared_card_xtts_makes_room_and_comes_back(server, monkeypatch):
    _write_sample("default-de-female")
    repos.set_setting(audiocpp.DEVICE_SETTING, "cuda:0")
    live = {"tts-xtts": {"assigned": "cuda:0", "effective": "cuda:0", "loaded": True}}
    calls = _fake_gpu_services(monkeypatch, live)
    await tts_engines.audiocpp_server()

    notes = await tts_engines.activate("audiocpp:qwen3-tts")

    assert calls == [("tts-xtts", "off")] and "XTTS entladen (gleiche Karte, cuda:0)." in notes
    assert gpu_manager.auto_off("tts-xtts") and tts_engines.fallback_engine() == "piper"

    # Zurueck zu XTTS: die audio.cpp-Stimme wird pausiert, XTTS wieder geladen.
    server.unload_calls.clear()
    calls.clear()
    notes = await tts_engines.activate("xtts")

    assert server.unload_calls == [["qwen3-tts"]] and calls == [("tts-xtts", "cuda:0")]
    assert "audio.cpp/qwen3-tts entladen (gleiche Karte, cuda:0)." in notes
    assert not gpu_manager.auto_off("tts-xtts") and tts_engines.fallback_engine() == "xtts"


async def test_leaving_audiocpp_pauses_the_previous_voice_on_any_card(server, monkeypatch):
    server.models["qwen3-tts"]["loaded"] = True
    await tts_engines.audiocpp_server()
    repos.set_setting(tts_engines.ACTIVE_ENGINE_SETTING, "audiocpp:qwen3-tts")
    _fake_gpu_services(monkeypatch, {"tts-xtts": {"assigned": "cuda:1", "effective": "cuda:1", "loaded": True}})

    notes = await tts_engines.activate("piper")

    assert server.unload_calls == [["qwen3-tts"]]
    assert notes == ["audio.cpp/qwen3-tts entladen (Hauptstimme gewechselt)."]
    assert server.models["kokoro"]["loaded"]  # andere Modelle bleiben, wie sie sind


async def test_unreachable_audiocpp_unloads_nothing(monkeypatch):
    repos.set_setting(audiocpp.URL_SETTING, URL)
    repos.set_setting(audiocpp.MODELS_SETTING, json.dumps([{"id": "qwen3-tts", "family": "qwen3_tts"}]))
    repos.set_setting(audiocpp.DEVICE_SETTING, "cuda:0")
    _unreachable(monkeypatch)
    calls = _fake_gpu_services(monkeypatch, {"tts-xtts": {"assigned": "cuda:0", "effective": "cuda:0",
                                                          "loaded": True}})

    notes = await tts_engines.activate("audiocpp:qwen3-tts")

    assert calls == [] and len(notes) == 1
    assert notes[0].startswith("audio.cpp ist nicht erreichbar") and notes[0].endswith("spricht XTTS.")
    assert tts_engines.active_engine() == "audiocpp:qwen3-tts"


async def test_a_failing_test_sentence_still_counts_as_loaded(monkeypatch):
    # Qwen3-Base ohne Sample: audio.cpp laedt das Modell, lehnt den Satz aber ab.
    fake = FakeAudioCpp()

    def reject(request):
        if request.url.path == "/v1/audio/speech":
            fake.models["qwen3-tts"]["loaded"] = True
            return fake.error(500, "Qwen3 TTS Base requires reference audio")
        return fake(request)

    repos.set_setting(audiocpp.URL_SETTING, URL)
    _mock_http(monkeypatch, reject)
    await tts_engines.audiocpp_server()

    notes = await tts_engines.activate("audiocpp:qwen3-tts")

    assert notes[-1].startswith("audio.cpp/qwen3-tts geladen - der Testsatz scheiterte aber:")


# ---- Panel ------------------------------------------------------------------------------------


def test_panel_lists_models_voices_and_settings(client, admin_headers, server):
    data = client.get("/v1/admin/tts", headers=admin_headers).json()

    section = data["audiocpp"]
    assert section["reachable"] and section["url_effective"] == URL and section["backend"] == "cuda"
    models = {m["id"]: m for m in section["models"]}
    assert models["kokoro"]["voices"] == ["af_heart", "ff_siwis"] and models["kokoro"]["loaded"]
    assert models["qwen3-asr"]["engine_id"] is None
    engines = {e["id"]: e for e in data["engines"]}
    assert engines["audiocpp:kokoro"]["status"]["status"] == "ok" and engines["audiocpp:kokoro"]["audiocpp"]

    saved = client.put("/v1/admin/tts/settings", headers=admin_headers,
                       json={"audiocpp_url": "audiocpp-server:8080", "audiocpp_device": "cuda:1"}).json()
    assert saved["audiocpp_url_effective"] == "http://audiocpp-server:8080" and saved["audiocpp_device"] == "cuda:1"
    assert client.put("/v1/admin/tts/settings", headers=admin_headers,
                      json={"audiocpp_device": "gpu1"}).status_code == 422
    assert client.put("/v1/admin/tts/settings", headers=admin_headers,
                      json={"audiocpp_url": "ftp://x"}).status_code == 400

    voice = client.put("/v1/admin/tts/audiocpp/voice", headers=admin_headers,
                       json={"model": "kokoro", "voice": "af_heart"})
    assert voice.json() == {"model": "kokoro", "voice": "af_heart"}
    assert audiocpp.voice_choices() == {"kokoro": "af_heart"}


def test_a_new_address_forgets_the_old_models(client, admin_headers, server):
    client.get("/v1/admin/tts", headers=admin_headers)
    assert "audiocpp:kokoro" in tts_engines.engine_ids()

    client.put("/v1/admin/tts/settings", headers=admin_headers, json={"audiocpp_url": "http://anderswo:8080"})

    assert not any(e.startswith("audiocpp:") for e in tts_engines.engine_ids())


def test_panel_unloads_all_audiocpp_voices(client, admin_headers, server):
    response = client.post("/v1/admin/tts/audiocpp/unload", headers=admin_headers)

    assert response.json() == {"unloaded": ["kokoro"]} and server.unload_calls == [["kokoro"]]


def test_filler_dropdown_picks_up_new_models(client, admin_headers, server):
    server.models["neu-tts"] = {"family": "supertonic", "task": "tts", "mode": "offline", "loaded": False}

    engines = client.get("/v1/admin/tts-engines", headers=admin_headers).json()

    assert "audiocpp:neu-tts" in {e["id"] for e in engines}


def test_preview_explains_an_unreachable_server(client, admin_headers, monkeypatch):
    repos.set_setting(audiocpp.URL_SETTING, URL)
    repos.set_setting(audiocpp.MODELS_SETTING, json.dumps([{"id": "qwen3-tts", "family": "qwen3_tts"}]))
    _unreachable(monkeypatch)

    response = client.post("/v1/admin/tts/preview", headers=admin_headers,
                           json={"engine": "audiocpp:qwen3-tts", "voice_id": "default-de-female", "text": "Hallo"})

    assert response.status_code == 502
    assert "Server nicht gefunden" in response.json()["detail"] and "audio.cpp" in response.json()["detail"]


def test_breeze_and_qwen3_are_no_longer_built_in(client, admin_headers):
    """v1.22: Beide laufen ueber audio.cpp - keine eigene Engine, keine
    GPU-Zeile, keine Einstellungen mehr."""
    assert "breeze" not in tts_engines.engine_ids() and "qwen3" not in tts_engines.engine_ids()
    assert "tts-breeze" not in gpu_manager.services() and "tts-qwen3" not in gpu_manager.services()
    assert not any(key.startswith("breeze") for key in client.get("/v1/admin/tts", headers=admin_headers).json())


async def test_fillers_can_be_spoken_by_an_audiocpp_model(server):
    for voice in ("default-de-female", "default-de-male"):
        _write_sample(voice)
    server.rate = 24000
    await tts_engines.audiocpp_server()

    trigger = repos.create_trigger("T-audiocpp", "thinking", None)
    filler = repos.create_filler("Titel", "Moment bitte, ich schaue nach.", trigger["id"], True,
                                 delay_ms=0, engine="audiocpp:kokoro")

    results = await filler_service.generate_audio(filler["id"])

    assert results and all(result["ok"] for result in results), results
    assert filler_service.has_audio(filler["id"], "default-de-male")


async def test_gpu_overview_shows_what_audiocpp_has_loaded(server, monkeypatch):
    repos.set_setting(audiocpp.DEVICE_SETTING, "cuda:1")
    monkeypatch.setattr(gpu_manager, "service_status", AsyncMock(return_value={"reachable": False}))

    rows = {row["name"]: row for row in (await gpu_manager.overview())["services"]}

    row = rows["tts-audiocpp"]
    assert row["effective"] == "cuda:1" and row["loaded"] and row["detail"] == "geladen: kokoro"
    assert not row["controllable"]


def test_split_sentences_keeps_short_bits_together():
    assert split_sentences("Es ist 3. Oktober. Ja!") == ["Es ist 3. Oktober. Ja!"]
    assert split_sentences("Erster Satz ist lang genug. Zweiter Satz ist auch lang genug.") == [
        "Erster Satz ist lang genug.", "Zweiter Satz ist auch lang genug."]
    assert split_sentences("   ") == []
