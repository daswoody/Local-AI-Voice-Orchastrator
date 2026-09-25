"""Fake-Backends fuer lokale Orchestrator-Tests ohne GPU/echte Infra.

Startet EINEN Server (Port 9100), der LiteLLM, STT, Piper, XTTS und Breeze
TTS 2 als Sub-Apps unter eigenen Pfad-Prefixen nachstellt. Damit laesst sich
der komplette Voice-Loop (Mikro-Phase 1.11) inklusive TTS-Engine-Wechsel
(v1.17) auf jedem Rechner durchspielen:

    Terminal 1:  uv run python scripts/dev_fake_services.py
    Terminal 2:  LITELLM_BASE_URL=http://127.0.0.1:9100/llm \\
                 STT_BASE_URL=http://127.0.0.1:9100/stt \\
                 PIPER_BASE_URL=http://127.0.0.1:9100/piper \\
                 XTTS_BASE_URL=http://127.0.0.1:9100/xtts \\
                 BREEZE_BASE_URL=http://127.0.0.1:9100/breeze \\
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


app = FastAPI(title="Heim-AI Fake-Backends")
app.mount("/llm", llm)
app.mount("/stt", stt)
app.mount("/piper", piper)
app.mount("/xtts", xtts)
app.mount("/breeze", breeze)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9100)
