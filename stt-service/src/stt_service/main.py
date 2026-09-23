import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

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


# ---- GPU-/Device-Zuweisung (v1.14) ------------------------------------------
#
# Der Container sieht alle Karten (compose: count all); WELCHE genutzt wird,
# entscheidet das Admin-Panel zur Laufzeit. Das Modell wird dabei auf der
# neuen Karte neu geladen - kein Container-Neustart noetig.


class DevicePayload(BaseModel):
    # "cpu", "cuda", "cuda:0", "cuda:1", ...
    device: str = Field(pattern=r"^(cpu|cuda(:\d+)?)$")
    # Optionaler Hinweis des Orchestrators: Karten vor Turing (Pascal &
    # aelter) rechnen float16 langsam - dort passt int8_float16 besser.
    compute_type: str | None = None


@app.get("/v1/device")
def get_device() -> dict:
    return engine_module.engine.status()


@app.post("/v1/device")
async def set_device(payload: DevicePayload) -> dict:
    """Laedt das Modell auf dem gewuenschten Device neu. Blockierend (GPU),
    daher im Threadpool - kann je nach Modellgroesse einige Sekunden
    dauern."""
    try:
        return await run_in_threadpool(
            engine_module.engine.set_device, payload.device, payload.compute_type
        )
    except Exception as exc:
        logger.exception("Device-Wechsel auf %s fehlgeschlagen", payload.device)
        raise HTTPException(
            status_code=500, detail=f"Device-Wechsel fehlgeschlagen: {str(exc)[:300]}"
        )


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
    try:
        return await run_in_threadpool(engine_module.engine.transcribe, pcm)
    except Exception as exc:
        # Klartext statt anonymem 500: der Orchestrator loggt das Detail,
        # und `docker logs heimai-stt` hat den vollen Traceback.
        logger.exception("Transkription fehlgeschlagen")
        raise HTTPException(status_code=500, detail=f"Transkription fehlgeschlagen: {str(exc)[:300]}")
