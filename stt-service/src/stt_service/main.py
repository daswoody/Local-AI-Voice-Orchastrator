from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from .audio import TARGET_RATE, resample_pcm16, wav_to_pcm16
from .config import settings
from . import engine as engine_module


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.preload:
        await run_in_threadpool(engine_module.engine.load)
    yield


app = FastAPI(title="Heim-AI STT (faster-whisper)", lifespan=lifespan)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "heimai-stt", "docs": "/docs", "health": "/v1/health"}


@app.get("/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/transcribe")
async def transcribe(request: Request, sample_rate: int = TARGET_RATE) -> dict:
    """Audio rein -> Text raus (Erfolgskriterium 1.8).

    Body ist entweder eine komplette WAV-Datei (RIFF-Header wird erkannt,
    Samplerate/Kanaele aus dem Header) oder rohes PCM16 mono - dann gibt der
    Query-Parameter sample_rate die Rate an (Default 16k, wie der
    Orchestrator es laut Protokoll liefert)."""
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="leerer Audio-Body")

    if body[:4] == b"RIFF":
        try:
            pcm, src_rate = wav_to_pcm16(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"WAV nicht lesbar: {exc}") from exc
    else:
        pcm, src_rate = body, sample_rate

    pcm = resample_pcm16(pcm, src_rate)

    # Whisper-Inferenz ist CPU-/GPU-gebunden und blockierend -> Threadpool,
    # damit der Event-Loop (Healthchecks, parallele Requests) frei bleibt.
    return await run_in_threadpool(engine_module.engine.transcribe, pcm)
