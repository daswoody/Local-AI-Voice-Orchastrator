"""Fake-Backends fuer lokale Orchestrator-Tests ohne GPU/echte Infra.

Startet EINEN Server (Port 9100), der LiteLLM, STT, Piper und XTTS als
Sub-Apps unter eigenen Pfad-Prefixen nachstellt. Damit laesst sich der
komplette Voice-Loop (Mikro-Phase 1.11) auf jedem Rechner durchspielen:

    Terminal 1:  uv run python scripts/dev_fake_services.py
    Terminal 2:  LITELLM_BASE_URL=http://127.0.0.1:9100/llm \\
                 STT_BASE_URL=http://127.0.0.1:9100/stt \\
                 PIPER_BASE_URL=http://127.0.0.1:9100/piper \\
                 XTTS_BASE_URL=http://127.0.0.1:9100/xtts \\
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
from fastapi import FastAPI, Response
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


@xtts.post("/v1/synthesize")
async def xtts_synthesize(payload: dict) -> StreamingResponse:
    async def generate():
        for freq in (440, 550, 660):
            yield _sine_pcm16(0.3, 24000, freq=freq)
            await asyncio.sleep(0.05)

    return StreamingResponse(generate(), media_type="application/octet-stream",
                             headers={"X-Sample-Rate": "24000"})


app = FastAPI(title="Heim-AI Fake-Backends")
app.mount("/llm", llm)
app.mount("/stt", stt)
app.mount("/piper", piper)
app.mount("/xtts", xtts)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9100)
