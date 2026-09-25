"""HTTP-Seite des Engine-Vertrags v1 - fuer jede Engine gleich.

    GET  /v1/health      Container lebt (+ Zustand)
    GET  /v1/info        Steckbrief: Name, Sprachen, Samplerate, braucht Sample? ...
    GET  /v1/device      {assigned, effective, loaded, state, detail, model}
    POST /v1/device      {"device": "off" | "cpu" | "cuda" | "cuda:N"} - laden/entladen
    POST /v1/synthesize  Multipart: text, language, ref_audio (WAV), ref_text,
                         instruction, voice_id, load_timeout_s
                         -> PCM16-Stream (mono) mit Header X-Sample-Rate

Fehler vor dem ersten Audio kommen als saubere HTTP-Codes (400 Anfrage, 409
belegt, 503 aus/laedt, 500 Engine) statt als abgerissener Stream: Der erste
Chunk wird erzeugt, bevor die Antwort-Header rausgehen."""

import hashlib
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .worker import (
    DEVICE_PATTERN,
    EngineBadRequest,
    EngineBusy,
    EngineFailed,
    EngineNotReady,
    EngineOff,
    EngineWorker,
    WorkerSettings,
    load_backend_class,
)

logger = logging.getLogger(__name__)

CONTRACT_VERSION = 1
# Laenger als 10 min wartet niemand auf einen Ladevorgang.
_MAX_LOAD_TIMEOUT_S = 600.0


class DevicePayload(BaseModel):
    device: str = Field(pattern=DEVICE_PATTERN)


def create_app(backend_path: str, settings: WorkerSettings | None = None) -> FastAPI:
    """App fuer die Backend-Klasse unter `backend_path` ("paket.modul:Klasse")."""
    settings = settings or WorkerSettings.from_env()
    backend_cls = load_backend_class(backend_path)
    info = backend_cls.info()
    worker = EngineWorker(backend_path, backend_cls, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker.start()
        try:
            yield
        finally:
            await run_in_threadpool(worker.shutdown)

    app = FastAPI(title=f"Heim-AI TTS-Engine: {info.name}", lifespan=lifespan)
    app.state.worker = worker

    @app.get("/")
    def root() -> dict:
        return {"service": info.name, "contract": CONTRACT_VERSION, "docs": "/docs",
                "health": "/v1/health"}

    @app.get("/v1/health")
    def health() -> dict:
        return {"status": "ok", "state": worker.state}

    @app.get("/v1/info")
    def get_info() -> dict:
        return {**asdict(info), "languages": list(info.languages), "contract": CONTRACT_VERSION}

    @app.get("/v1/device")
    def get_device() -> dict:
        return worker.status()

    @app.post("/v1/device")
    async def set_device(payload: DevicePayload) -> dict:
        try:
            return await run_in_threadpool(worker.set_device, payload.device)
        except EngineBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/v1/synthesize")
    async def synthesize(
        text: str = Form(...),
        language: str | None = Form(None),
        voice_id: str | None = Form(None),
        ref_text: str | None = Form(None),
        instruction: str | None = Form(None),
        load_timeout_s: float = Form(0.0),
        ref_audio: UploadFile | None = File(None),
    ) -> StreamingResponse:
        text = text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Text fehlt")
        voice = None
        if ref_audio is not None:
            wav = await ref_audio.read()
            if wav:
                transcript = (ref_text or "").strip() or None
                key = hashlib.sha256(wav + b"\0" + (transcript or "").encode()).hexdigest()
                voice = {"key": key, "wav": wav, "transcript": transcript}
        job = {
            "text": text,
            "language": (language or "").strip().lower() or None,
            "voice": voice,
            "voice_id": voice_id,
            "instruction": (instruction or "").strip() or None,
        }
        stream = worker.synthesize(job, min(max(load_timeout_s, 0.0), _MAX_LOAD_TIMEOUT_S))
        try:
            first = await run_in_threadpool(next, stream, None)
        except (EngineOff, EngineNotReady) as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except EngineBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except EngineBadRequest as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except EngineFailed as exc:
            raise HTTPException(status_code=500, detail=f"{info.name}: {exc}")
        if first is None:
            raise HTTPException(status_code=500, detail=f"{info.name} hat kein Audio erzeugt")
        rate, first_chunk = first

        async def body():
            try:
                yield first_chunk
                while (item := await run_in_threadpool(next, stream, None)) is not None:
                    yield item[1]
            except Exception:
                # Header sind raus - der Abbruch ist nicht mehr zu verhindern,
                # aber der Grund gehoert ins Log.
                logger.exception("Stream mitten in der Synthese abgerissen")
                raise
            finally:
                # Client weg: Die Synthese stoppt nach dem laufenden Chunk.
                await run_in_threadpool(stream.close)

        return StreamingResponse(
            body(),
            media_type="application/octet-stream",
            headers={"X-Sample-Rate": str(rate), "X-Sample-Format": "s16le"},
        )

    return app
