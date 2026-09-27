"""Fake-Backends fuer lokale Orchestrator-Tests ohne GPU/echte Infra.

Startet EINEN Server (Port 9100), der LiteLLM, STT, Piper, XTTS, Breeze
TTS 2, eine Engine nach dem Engine-Vertrag (Qwen3) und einen audio.cpp-Server
(v1.21) als Sub-Apps unter eigenen Pfad-Prefixen nachstellt. Damit laesst
sich der komplette Voice-Loop (Mikro-Phase 1.11) inklusive TTS-Engine-Wechsel
(v1.17) auf jedem Rechner durchspielen:

    Terminal 1:  uv run python scripts/dev_fake_services.py
    Terminal 2:  LITELLM_BASE_URL=http://127.0.0.1:9100/llm \\
                 STT_BASE_URL=http://127.0.0.1:9100/stt \\
                 PIPER_BASE_URL=http://127.0.0.1:9100/piper \\
                 XTTS_BASE_URL=http://127.0.0.1:9100/xtts \\
                 BREEZE_BASE_URL=http://127.0.0.1:9100/breeze \\
                 QWEN3_BASE_URL=http://127.0.0.1:9100/qwen3 \\
                 AUDIOCPP_BASE_URL=http://127.0.0.1:9100/audiocpp \\
                 WEAVIATE_URL=http://127.0.0.1:9100/weaviate-gibtsnicht \\
                 uv run uvicorn orchestrator.main:app --port 8000
    Terminal 3:  uv run python scripts/manual_audio_ws_test.py
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


# --- Fake Breeze TTS 2 (Test-Engine, v1.17) ------------------------------------
# Formular-Signatur wie im offiziellen Server (breeze_infer/api.py), damit
# der Multipart-Request des Orchestrators gegen denselben Parser laeuft.
breeze = FastAPI()


@breeze.get("/health")
async def breeze_health() -> dict:
    return {"status": "ok", "sample_rate": 24000}


@breeze.post("/v1/audio/speech")
async def breeze_speech(
    text: str = Form(...),
    instruction: str | None = Form(None),
    cfg_scale: float = Form(1.0),
    ref_audio: UploadFile | None = File(None),
    ref_text: str = Form(""),
    seed: int = Form(42),
) -> StreamingResponse:
    has_reference = ref_audio is not None and bool(ref_audio.filename)
    if has_reference != bool(ref_text.strip()):
        raise HTTPException(
            status_code=400,
            detail="ref_audio and ref_text must be provided together or both omitted.",
        )
    # Klon = tieferer Ton, eingebaute Stimme = hoeherer - so hoert man im
    # Probehoeren, welcher Modus gegriffen hat. Laenge grob wie gesprochen
    # (~14 Zeichen/s), damit die Plausibilitaetspruefung der Filler passt.
    base = 220 if has_reference else 330
    part = (0.2 + len(text) / 14) / 3

    async def generate():
        for step in range(3):
            yield _sine_pcm16(part, 24000, freq=base + 110 * step)
            await asyncio.sleep(0.05)

    return StreamingResponse(generate(), media_type="audio/pcm",
                             headers={"X-Sample-Rate": "24000", "X-Sample-Format": "s16le"})


# --- Fake-Engine nach dem Engine-Vertrag (Qwen3-TTS, v1.20) ---------------------
# Zustaende wie tts-engine-kit: idle -> (Laden) -> ready, off; Synthese im
# Zustand idle laedt, wartet aber nur mit load_timeout_s > 0 darauf.
qwen3 = FastAPI()
_qwen3 = {"assigned": "cuda:0", "state": "idle"}


@qwen3.get("/v1/health")
async def qwen3_health() -> dict:
    return {"status": "ok", "state": _qwen3["state"]}


@qwen3.get("/v1/info")
async def qwen3_info() -> dict:
    return {"name": "Qwen3-TTS 1.7B (Fake)", "languages": ["de", "en"], "sample_rate": 24000,
            "needs_sample": True, "uses_transcript": True, "instructions": False,
            "streaming": "sentence", "vram_mb": 5000, "contract": 1,
            "description": "Fake-Engine nach dem Engine-Vertrag."}


def _qwen3_status() -> dict:
    loaded = _qwen3["state"] == "ready"
    return {"assigned": _qwen3["assigned"], "effective": _qwen3["assigned"] if loaded else None,
            "loaded": loaded, "state": _qwen3["state"], "detail": "", "model": "Fake"}


@qwen3.get("/v1/device")
async def qwen3_device() -> dict:
    return _qwen3_status()


@qwen3.post("/v1/device")
async def qwen3_set_device(payload: dict) -> dict:
    _qwen3["assigned"] = payload["device"]
    if payload["device"] == "off":
        _qwen3["state"] = "off"
    else:
        await asyncio.sleep(0.5)  # "laedt"
        _qwen3["state"] = "ready"
    return _qwen3_status()


@qwen3.post("/v1/synthesize")
async def qwen3_synthesize(
    text: str = Form(...),
    language: str | None = Form(None),
    ref_audio: UploadFile | None = File(None),
    ref_text: str | None = Form(None),
    load_timeout_s: float = Form(0.0),
) -> StreamingResponse:
    if _qwen3["state"] == "off":
        raise HTTPException(503, "Engine ist im Admin-Panel ausgeschaltet (GPUs)")
    if _qwen3["state"] != "ready":
        if load_timeout_s <= 0:
            _qwen3["state"] = "ready"  # "laedt im Hintergrund"
            raise HTTPException(503, "Modell wird geladen - gleich noch einmal versuchen")
        await asyncio.sleep(0.5)
        _qwen3["state"] = "ready"
    if ref_audio is None:
        raise HTTPException(400, "Qwen3-TTS braucht ein Voice-Sample")
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
    await asyncio.sleep(0.2)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(_sine_pcm16(0.2 + len(payload.get("input", "")) / 14, rate, freq=freq))
    return Response(buffer.getvalue(), media_type="audio/wav")


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
app.mount("/breeze", breeze)
app.mount("/qwen3", qwen3)
app.mount("/audiocpp", audiocpp)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9100)
