import io
import logging
import wave
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from . import engine as engine_module
from .config import settings
from .engine import SAMPLE_RATE, EngineBusy

logger = logging.getLogger(__name__)


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


class FullSynthesizeRequest(SynthesizeRequest):
    # Optional vorsichtiger sampeln (Neuversuch der Filler-Generierung);
    # None = XTTS-Default.
    temperature: float | None = Field(default=None, gt=0.0, le=1.5)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "heimai-tts-xtts", "docs": "/docs", "health": "/v1/health"}


@app.get("/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---- GPU-/Device-Zuweisung (v1.14) ------------------------------------------


class DevicePayload(BaseModel):
    device: str = Field(pattern=r"^(cpu|cuda(:\d+)?)$")


@app.get("/v1/device")
def get_device() -> dict:
    return engine_module.engine.status()


@app.post("/v1/device")
async def set_device(payload: DevicePayload) -> dict:
    """Laedt XTTS auf dem gewuenschten Device neu. Dauert einige Sekunden
    (Modell + Latents), laeuft deshalb im Threadpool."""
    try:
        return await run_in_threadpool(engine_module.engine.set_device, payload.device)
    except Exception as exc:
        logger.exception("Device-Wechsel auf %s fehlgeschlagen", payload.device)
        raise HTTPException(
            status_code=500, detail=f"Device-Wechsel fehlgeschlagen: {str(exc)[:300]}"
        )


@app.get("/v1/voices")
def list_voices() -> list[dict[str, str]]:
    """Verfuegbare Stimmen = vorhandene Sample-WAVs im Voices-Volume."""
    return [{"id": voice_id} for voice_id in engine_module.engine.list_voices()]


def _validate(payload: SynthesizeRequest) -> None:
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="leerer Text")
    if not engine_module.engine.has_voice(payload.voice_id):
        available = engine_module.engine.list_voices()
        raise HTTPException(
            status_code=404,
            detail=f"voice_id '{payload.voice_id}' unbekannt - verfuegbar: {available}",
        )


def _busy(exc: EngineBusy) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@app.post("/v1/synthesize")
async def synthesize(payload: SynthesizeRequest) -> StreamingResponse:
    """Text rein -> PCM16-Stream (mono, 24 kHz) raus (Erfolgskriterium 1.10).

    Roh-PCM statt WAV, weil WAV die Gesamtlaenge im Header braucht -
    Streaming waere damit nicht moeglich. Die Samplerate ist bei XTTS-v2
    fix und reist im X-Sample-Rate-Header mit."""
    _validate(payload)

    stream = engine_module.engine.stream(payload.text, payload.voice_id, payload.language)

    # Den ERSTEN Chunk vor der Response erzeugen (Threadpool, GPU-blockierend):
    # Modell-Laden, Latents-Berechnung und kaputte Samples schlagen genau dort
    # fehl - so werden daraus saubere 500er mit Fehlertext statt mitten im
    # Stream abgerissener Verbindungen ("incomplete chunked read" im Client).
    # Hier wartet der Request auch, falls gerade eine andere Synthese laeuft.
    try:
        first_chunk = await run_in_threadpool(next, stream, None)
    except EngineBusy as exc:
        raise _busy(exc)
    except Exception as exc:
        logger.exception("XTTS-Synthese fuer voice_id=%s fehlgeschlagen", payload.voice_id)
        raise HTTPException(status_code=500, detail=f"XTTS-Synthese fehlgeschlagen: {str(exc)[:300]}")
    if first_chunk is None:
        raise HTTPException(status_code=500, detail="XTTS hat keine Audio-Daten erzeugt")

    return StreamingResponse(
        _first_then_rest(first_chunk, stream),
        media_type="application/octet-stream",
        headers={"X-Sample-Rate": str(SAMPLE_RATE)},
    )


@app.post("/v1/synthesize/full")
async def synthesize_full(payload: FullSynthesizeRequest) -> Response:
    """Text rein -> komplettes WAV raus (v1.16, Filler-Vorgenerierung).

    Nicht streamend: Der Aufrufer wartet ohnehin aufs Ganze, dafuer liefert
    XTTS hier seinen Qualitaetspfad (siehe XttsEngine.synthesize)."""
    _validate(payload)
    try:
        pcm = await run_in_threadpool(
            engine_module.engine.synthesize,
            payload.text, payload.voice_id, payload.language, payload.temperature,
        )
    except EngineBusy as exc:
        raise _busy(exc)
    except Exception as exc:
        logger.exception("XTTS-Synthese (full) fuer voice_id=%s fehlgeschlagen", payload.voice_id)
        raise HTTPException(status_code=500, detail=f"XTTS-Synthese fehlgeschlagen: {str(exc)[:300]}")
    if not pcm:
        raise HTTPException(status_code=500, detail="XTTS hat keine Audio-Daten erzeugt")
    return Response(content=_wav_bytes(pcm, SAMPLE_RATE), media_type="audio/wav")


def _first_then_rest(first_chunk: bytes, rest):
    try:
        yield first_chunk
        yield from rest
    except Exception:
        # Header sind raus, der Abbruch ist nicht mehr zu verhindern - aber
        # der Grund muss in den Container-Logs stehen.
        logger.exception("XTTS-Stream mitten in der Generierung abgerissen")
        raise
    finally:
        # Wird der Stream geschlossen (Client-Abbruch), bricht close() die
        # Generierung beim naechsten Chunk ab. Schliesst ihn niemand, gibt
        # der Producer-Thread die Engine trotzdem mit Generierungsende frei.
        rest.close()


def _wav_bytes(pcm: bytes, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()
