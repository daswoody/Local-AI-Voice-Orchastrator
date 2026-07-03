from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import engine as engine_module
from .config import settings
from .engine import SAMPLE_RATE


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.preload:
        await run_in_threadpool(engine_module.engine.load)
    yield


app = FastAPI(title="Heim-AI TTS XTTS-v2 (Hauptstimme)", lifespan=lifespan)


class SynthesizeRequest(BaseModel):
    text: str
    voice_id: str
    language: str | None = None


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "heimai-tts-xtts", "docs": "/docs", "health": "/v1/health"}


@app.get("/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/voices")
def list_voices() -> list[dict[str, str]]:
    """Verfuegbare Stimmen = vorhandene Sample-WAVs im Voices-Volume."""
    return [{"id": voice_id} for voice_id in engine_module.engine.list_voices()]


@app.post("/v1/synthesize")
def synthesize(payload: SynthesizeRequest) -> StreamingResponse:
    """Text rein -> PCM16-Stream (mono, 24 kHz) raus (Erfolgskriterium 1.10).

    Roh-PCM statt WAV, weil WAV die Gesamtlaenge im Header braucht -
    Streaming waere damit nicht moeglich. Die Samplerate ist bei XTTS-v2
    fix und reist im X-Sample-Rate-Header mit."""
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="leerer Text")
    if not engine_module.engine.has_voice(payload.voice_id):
        available = engine_module.engine.list_voices()
        raise HTTPException(
            status_code=404,
            detail=f"voice_id '{payload.voice_id}' unbekannt - verfuegbar: {available}",
        )

    # Sync-Generator: StreamingResponse iteriert ihn im Threadpool, der
    # Event-Loop bleibt frei waehrend die GPU rechnet.
    stream = engine_module.engine.stream(payload.text, payload.voice_id, payload.language)
    return StreamingResponse(
        stream,
        media_type="application/octet-stream",
        headers={"X-Sample-Rate": str(SAMPLE_RATE)},
    )
