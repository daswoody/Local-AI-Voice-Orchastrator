"""Fake-Backends fuer lokale Orchestrator-Tests ohne GPU/echte Infra.

Startet EINEN Server (Port 9100), der LiteLLM, STT, Piper, XTTS, einen
audio.cpp-Server (v1.21) und eine Engine nach dem Engine-Vertrag als Sub-Apps
unter eigenen Pfad-Prefixen nachstellt. Damit laesst sich der komplette
Voice-Loop (Mikro-Phase 1.11) inklusive TTS-Engine-Wechsel (v1.17) auf jedem
Rechner durchspielen:

    Terminal 1:  uv run python scripts/dev_fake_services.py
    Terminal 2:  LITELLM_BASE_URL=http://127.0.0.1:9100/llm \\
                 STT_BASE_URL=http://127.0.0.1:9100/stt \\
                 PIPER_BASE_URL=http://127.0.0.1:9100/piper \\
                 XTTS_BASE_URL=http://127.0.0.1:9100/xtts \\
                 AUDIOCPP_BASE_URL=http://127.0.0.1:9100/audiocpp \\
                 WEAVIATE_URL=http://127.0.0.1:9100/weaviate-gibtsnicht \\
                 uv run uvicorn orchestrator.main:app --port 8000
    Terminal 3:  uv run python scripts/manual_audio_ws_test.py

Die Fake-Vertrags-Engine traegt man bei Bedarf im Admin-Panel unter
Sprachausgabe -> Engines nach dem Engine-Vertrag ein (Adresse
http://127.0.0.1:9100/engine).
"""

import asyncio
import io
import math
import struct
import wave

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse


def _sine_pcm16(seconds: float, rate: int, freq: float = 440.0) -> bytes:
    samples = (
        int(20000 * math.sin(2 * math.pi * freq * i / rate))
        for i in range(int(rate * seconds))
    )
    return b"".join(struct.pack("<h", s) for s in samples)


# --- Fake LiteLLM ------------------------------------------------------------
llm = FastAPI()


@llm.get("/v1/models")
async def list_models() -> dict:
    return {"object": "list", "data": [{"id": "gemma-4-e4b"}, {"id": "qwen3-8b"}]}


@llm.post("/v1/chat/completions")
async def chat_completions(payload: dict) -> dict:
    messages = payload.get("messages", [])
    user_text = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
    )
    has_tool_result = any(m.get("role") == "tool" for m in messages)

    # Tool-Demo (1.12): Enthaelt die Frage "karte", ruft das Fake-LLM das
    # show_card-Builtin auf - komplett server-seitig, kein MCP noetig.
    if "karte" in user_text.lower() and payload.get("tools") and not has_tool_result:
        return {"choices": [{"message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_demo",
                "type": "function",
                "function": {
                    "name": "show_card",
                    "arguments": '{"card_type": "generic", "title": "Demo",'
                                 ' "data": {"headline": "Fake-Karte", "body": "Aus dem Tool-Loop."}}',
                },
            }],
        }}]}

    await asyncio.sleep(2.0)  # LLM-Latenz simulieren -> Filler-Logik wird sichtbar
    suffix = " (nach Tool-Aufruf)" if has_tool_result else ""
    return {"choices": [{"message": {
        "role": "assistant",
        "content": f"Fake-Antwort auf: {user_text}{suffix}",
    }}]}


# --- Fake STT ----------------------------------------------------------------
stt = FastAPI()


@stt.post("/v1/transcribe")
async def transcribe() -> dict:
    return {"text": "Dies ist ein Fake-Transkript.", "language": "de",
            "audio_duration_ms": 1000, "processing_ms": 3}


# --- Fake Piper (Filler, 22050 Hz WAV) ----------------------------------------
piper = FastAPI()


@piper.get("/v1/health")
async def piper_health() -> dict:
    return {"status": "ok"}


@piper.post("/v1/synthesize")
async def piper_synthesize(payload: dict) -> Response:
    rate = 22050
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(_sine_pcm16(0.4, rate, freq=330))
    return Response(buffer.getvalue(), media_type="audio/wav",
                    headers={"X-Sample-Rate": str(rate)})


# --- Fake XTTS (Hauptstimme, 24k PCM-Stream) -----------------------------------
xtts = FastAPI()


# Zuweisung wie im echten Dienst (v1.14): "cuda:N", "cpu" oder "off" (v1.19).
_xtts_device = {"assigned": "cuda:0"}


@xtts.get("/v1/health")
async def xtts_health() -> dict:
    return {"status": "ok"}


@xtts.get("/v1/device")
async def xtts_device() -> dict:
    assigned = _xtts_device["assigned"]
    off = assigned == "off"
    return {"assigned": assigned, "effective": None if off else assigned,
            "loaded": not off, "model": "fake-xtts"}


@xtts.post("/v1/device")
async def xtts_set_device(payload: dict) -> dict:
    _xtts_device["assigned"] = payload["device"]
    return await xtts_device()


def _xtts_off() -> None:
    if _xtts_device["assigned"] == "off":
        raise HTTPException(503, "XTTS ist im Admin-Panel ausgeschaltet (GPUs -> XTTS)")


@xtts.post("/v1/synthesize")
async def xtts_synthesize(payload: dict) -> StreamingResponse:
    _xtts_off()

    async def generate():
        for freq in (440, 550, 660):
            yield _sine_pcm16(0.3, 24000, freq=freq)
            await asyncio.sleep(0.05)

    return StreamingResponse(generate(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": "24000"})


@xtts.post("/v1/synthesize/full")
async def xtts_synthesize_full(payload: dict) -> Response:
    # Komplettes WAV fuer die Filler-Vorgenerierung (v1.16). Laenge grob wie
    # gesprochen (~14 Zeichen/s), damit die Plausibilitaetspruefung passt;
    # die Pause macht den Busy-Zustand im Panel sichtbar.
    _xtts_off()
    await asyncio.sleep(0.8)
    seconds = 0.2 + len(payload.get("text", "")) / 14
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(_sine_pcm16(seconds, 24000, freq=440))
    return Response(buffer.getvalue(), media_type="audio/wav")


# --- Fake-Engine nach dem Engine-Vertrag (v1.20) ----------------------------------
# Zustaende wie tts-engine-kit: idle -> (Laden) -> ready, off; Synthese im
# Zustand idle laedt, wartet aber nur mit load_timeout_s > 0 darauf.
engine = FastAPI()
_engine = {"assigned": "cuda:0", "state": "idle"}


@engine.get("/v1/health")
async def engine_health() -> dict:
    return {"status": "ok", "state": _engine["state"]}


@engine.get("/v1/info")
async def engine_info() -> dict:
    return {"name": "Demo-TTS (Fake)", "languages": ["de", "en"], "sample_rate": 24000,
            "needs_sample": True, "uses_transcript": True, "instructions": False,
            "streaming": "sentence", "vram_mb": 5000, "contract": 1,
            "description": "Fake-Engine nach dem Engine-Vertrag."}


def _engine_status() -> dict:
    loaded = _engine["state"] == "ready"
    return {"assigned": _engine["assigned"], "effective": _engine["assigned"] if loaded else None,
            "loaded": loaded, "state": _engine["state"], "detail": "", "model": "Fake"}


@engine.get("/v1/device")
async def engine_device() -> dict:
    return _engine_status()


@engine.post("/v1/device")
async def engine_set_device(payload: dict) -> dict:
    _engine["assigned"] = payload["device"]
    if payload["device"] == "off":
        _engine["state"] = "off"
    else:
        await asyncio.sleep(0.5)  # "laedt"
        _engine["state"] = "ready"
    return _engine_status()


@engine.post("/v1/synthesize")
async def engine_synthesize(
    text: str = Form(...),
    language: str | None = Form(None),
    ref_audio: UploadFile | None = File(None),
    ref_text: str | None = Form(None),
    load_timeout_s: float = Form(0.0),
) -> StreamingResponse:
    if _engine["state"] == "off":
        raise HTTPException(503, "Engine ist im Admin-Panel ausgeschaltet (GPUs)")
    if _engine["state"] != "ready":
        if load_timeout_s <= 0:
            _engine["state"] = "ready"  # "laedt im Hintergrund"
            raise HTTPException(503, "Modell wird geladen - gleich noch einmal versuchen")
        await asyncio.sleep(0.5)
        _engine["state"] = "ready"
    if ref_audio is None:
        raise HTTPException(400, "Die Demo-Engine braucht ein Voice-Sample")
    # Satz fuer Satz, Ton je nach Klon-Modus (mit Transkript tiefer).
    sentences = [part for part in text.replace("!", ".").replace("?", ".").split(".") if part.strip()]
    base = 200 if ref_text else 260

    async def generate():
        for index, sentence in enumerate(sentences or [text]):
            await asyncio.sleep(0.1)
            yield _sine_pcm16(0.2 + len(sentence) / 14, 24000, freq=base + 40 * index)

    return StreamingResponse(generate(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": "24000", "X-Sample-Format": "s16le"})


# --- Fake audio.cpp (v1.21) -------------------------------------------------------
# Gleiche Routen und Fehlerform wie audiocpp_server (app/server/runtime.cpp):
# Modelle laden beim ersten Request (lazy), Fehler als {"error": {...}} mit
# HTTP 500. qwen3-tts nimmt Sprachen nur beim Namen, kokoro kennt kein
# reference_text - genau die Faelle, auf die der Client reagieren muss.
# voxcpm2-stream streamt (v1.23): stream_format=sse liefert speech.audio.delta-
# Events mit rohem PCM16 (ohne Samplerate) im Takt der "Berechnung".
audiocpp = FastAPI()
_audiocpp_models = {
    "qwen3-tts": {"family": "qwen3_tts", "task": "tts", "mode": "offline", "loaded": False, "rate": 24000},
    "kokoro": {"family": "kokoro_tts", "task": "tts", "mode": "offline", "loaded": False, "rate": 24000,
               "voices": ["af_heart", "bf_emma", "ff_siwis"]},
    "voxcpm2-stream": {"family": "voxcpm2", "task": "tts", "mode": "streaming", "loaded": False, "rate": 48000},
    "qwen3-asr": {"family": "qwen3_asr", "task": "asr", "mode": "offline", "loaded": False},
}


def _audiocpp_error(status: int, message: str, kind: str = "server_error") -> Response:
    import json

    return Response(json.dumps({"error": {"message": message, "type": kind}}), status_code=status,
                    media_type="application/json")


@audiocpp.get("/health")
async def audiocpp_health() -> dict:
    return {"status": "ok", "backend": "cuda", "models": len(_audiocpp_models), "ui": True,
            "ui_management": False}


@audiocpp.get("/v1/models")
async def audiocpp_list() -> dict:
    return {"object": "list", "data": [
        {"id": model_id, "object": "model", "owned_by": "engine", "family": m["family"],
         "task": m["task"], "mode": m["mode"], "loaded": m["loaded"], "path": f"/models/{model_id}"}
        for model_id, m in _audiocpp_models.items()]}


@audiocpp.get("/v1/audio/voices")
async def audiocpp_voices(model: str = "") -> dict:
    return {"voices": _audiocpp_models.get(model, {}).get("voices", [])}


@audiocpp.post("/v1/audio/speech")
async def audiocpp_speech(payload: dict) -> Response:
    model_id = payload.get("model", "")
    model = _audiocpp_models.get(model_id)
    if model is None:
        return _audiocpp_error(500, f"unknown model id: {model_id}")
    streamed = "stream_format" in payload or payload.get("stream")
    if streamed and model["mode"] != "streaming":
        return _audiocpp_error(500, "speech streaming requires a model configured with mode=streaming")
    if streamed and payload.get("response_format", "pcm") != "pcm":
        return _audiocpp_error(500, "streaming speech currently supports response_format=pcm")
    if not model["loaded"]:
        await asyncio.sleep(1.5)  # "laedt"
        model["loaded"] = True
    language = payload.get("language", "")
    if model["family"] == "qwen3_tts" and language and language.lower() not in (
            "auto", "german", "english", "chinese"):
        return _audiocpp_error(500, f"Qwen3 talker unsupported language: {language}")
    if model["family"] == "kokoro_tts" and "reference_text" in payload:
        return _audiocpp_error(500, "unknown Kokoro request option: reference_text")
    if model["family"] == "qwen3_tts" and "voice_ref" not in payload:
        return _audiocpp_error(500, "Qwen3 TTS Base requires reference audio (voice_ref)")
    # Klon = tiefer, eingebaute Stimme = hoeher; Laenge grob wie gesprochen.
    freq = 220 if "voice_ref" in payload else 330
    rate = model["rate"]
    if streamed:
        return StreamingResponse(_audiocpp_events(payload, model, freq), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})
    await asyncio.sleep(0.2)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(_sine_pcm16(0.2 + len(payload.get("input", "")) / 14, rate, freq=freq))
    return Response(buffer.getvalue(), media_type="audio/wav")


async def _audiocpp_events(payload: dict, model: dict, freq: float):
    import base64
    import json

    def event(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    if model["family"] == "voxcpm2" and payload.get("options", {}).get("retry_badcase") is not False:
        yield event({"type": "error", "error": {"message": "VoxCPM2 streaming generation requires retry_badcase=false"}})
        return
    rate = model["rate"]
    pcm = _sine_pcm16(0.2 + len(payload.get("input", "")) / 14, rate, freq=freq)
    piece = int(rate * 0.08) * 2  # 80 ms je Delta, "berechnet" in 40 ms
    for offset in range(0, len(pcm), piece):
        await asyncio.sleep(0.04)
        yield event({"type": "speech.audio.delta", "audio": base64.b64encode(pcm[offset:offset + piece]).decode()})
    yield event({"type": "speech.audio.done", "timing": {"ttft_ms": 40.0}})
    yield "data: [DONE]\n\n"


@audiocpp.post("/v1/tasks/unload_models")
async def audiocpp_unload(payload: dict) -> dict:
    unloaded, not_found = [], []
    for model_id in payload.get("model_ids", []):
        model = _audiocpp_models.get(model_id)
        if model is None:
            not_found.append(model_id)
        elif model["loaded"]:
            model["loaded"] = False
            unloaded.append(model_id)
    return {"unloaded": unloaded, "not_found": not_found}


app = FastAPI(title="Heim-AI Fake-Backends")
app.mount("/llm", llm)
app.mount("/stt", stt)
app.mount("/piper", piper)
app.mount("/xtts", xtts)
app.mount("/engine", engine)
app.mount("/audiocpp", audiocpp)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9100)
