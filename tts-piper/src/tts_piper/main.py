import io
import wave

from fastapi import FastAPI, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import engine as engine_module

app = FastAPI(title="Heim-AI TTS Piper (Filler)")


class SynthesizeRequest(BaseModel):
    text: str


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "heimai-tts-piper", "docs": "/docs", "health": "/v1/health"}


@app.get("/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/synthesize")
async def synthesize(payload: SynthesizeRequest) -> Response:
    """Text rein -> WAV raus (Erfolgskriterium 1.9).

    WAV statt Roh-PCM, damit die Samplerate im Container-Format mitreist -
    der Orchestrator liest sie beim Parsen heraus und resampled den Filler
    auf die Stream-Rate (24k)."""
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="leerer Text")

    pcm, rate = await run_in_threadpool(engine_module.engine.synthesize, payload.text)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)

    return Response(
        content=buffer.getvalue(),
        media_type="audio/wav",
        headers={"X-Sample-Rate": str(rate)},
    )
