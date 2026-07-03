import asyncio
import contextlib
import logging
import random

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..audio import b64_to_pcm, chunk_pcm, pcm_to_b64, resample_pcm16
from ..config import settings
from ..graph import orchestrator_graph
from ..security import decode_access_token
from ..services.stt_client import stt_client
from ..services.tts_client import piper_client, xtts_client

logger = logging.getLogger(__name__)
router = APIRouter()

# Filler-Phrasen nach 4.3. Die Unterscheidung nach Trigger-Typ (Tool-Call vs.
# RAG/Suche) kommt mit dem Tool-Routing in 1.12 - bis dahin gibt es nur den
# "Nachdenk"-Fall.
_FILLER_PHRASES = [
    "Lass mich kurz nachdenken.",
    "Hm, einen Augenblick.",
    "Gib mir einen Moment.",
]


@router.websocket("/v1/assistant/stream")
async def assistant_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    session = StreamSession(websocket)
    try:
        while True:
            frame = await websocket.receive_json()
            await session.handle(frame)
    except WebSocketDisconnect:
        logger.info("Client hat die WebSocket-Verbindung getrennt")
    finally:
        await session.cancel_active(notify=False)


class StreamSession:
    """Zustand einer WebSocket-Verbindung: Modus/Stimme aus dem hello-Frame,
    Audio-Eingangspuffer und der gerade laufende Antwort-Task (fuer
    Barge-in/interrupt)."""

    def __init__(self, websocket: WebSocket) -> None:
        self.ws = websocket
        self.mode = "chat"
        self.voice_id = settings.default_voice_id
        self.tier = _resolve_tier(websocket)
        self.audio_buffer = bytearray()
        self.active_task: asyncio.Task | None = None

    # ---- Frame-Dispatch ----------------------------------------------------

    async def handle(self, frame: dict) -> None:
        frame_type = frame.get("type")

        if frame_type == "hello":
            self.mode = frame.get("mode", "chat")
            if frame.get("voice_id"):
                self.voice_id = frame["voice_id"]
            # Geraete-Tool-Manifest wird erst mit 1.12 als LLM-Tools
            # registriert (4.13).
            return

        if frame_type == "text_input":
            # Audio-Antwort nur in den Sprach-Modi; im Chat-Modus liest die
            # App per TTS-Fallback selbst vor (4.13).
            await self.start_response(frame.get("text", ""), want_audio=self.mode in ("talk", "assist"))
            return

        if frame_type == "audio_chunk":
            pcm = b64_to_pcm(frame.get("audio", ""))
            if len(self.audio_buffer) + len(pcm) > settings.max_audio_buffer_bytes:
                self.audio_buffer.clear()
                await self.ws.send_json({"type": "error", "message": "Audio-Puffer-Limit ueberschritten"})
                return
            self.audio_buffer.extend(pcm)
            return

        if frame_type == "audio_end":
            await self.finish_audio_input()
            return

        if frame_type == "interrupt":
            # Barge-in: laufende Antwort (LLM wie Audio-Stream) abbrechen.
            await self.cancel_active(notify=True)
            return

        await self.ws.send_json({"type": "error", "message": f"unbekannter frame type: {frame_type}"})

    async def finish_audio_input(self) -> None:
        pcm = bytes(self.audio_buffer)
        self.audio_buffer.clear()
        if not pcm:
            await self.ws.send_json({"type": "error", "message": "audio_end ohne Audio-Daten"})
            return

        try:
            text = await stt_client.transcribe(pcm, settings.input_sample_rate)
        except Exception:
            logger.exception("STT fehlgeschlagen")
            await self.ws.send_json({"type": "error", "message": "Spracherkennung fehlgeschlagen"})
            await self.ws.send_json({"type": "done"})
            return

        # Nur final: Partial-Transcripts brauchen Streaming-STT, das kommt
        # spaeter (bewusste v1-Einschraenkung, siehe README).
        await self.ws.send_json({"type": "transcript", "text": text, "final": True})

        if not text.strip():
            await self.ws.send_json({"type": "done"})
            return

        # Gesprochene Frage -> gesprochene Antwort, unabhaengig vom Modus.
        await self.start_response(text, want_audio=True)

    # ---- Antwort-Pipeline --------------------------------------------------

    async def start_response(self, text: str, want_audio: bool) -> None:
        # Neuer Input waehrend eine Antwort laeuft = implizites Barge-in.
        await self.cancel_active(notify=False)
        self.active_task = asyncio.create_task(self._respond(text, want_audio))

    async def cancel_active(self, notify: bool) -> None:
        task = self.active_task
        self.active_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if notify:
            # done schliesst den abgebrochenen Turn app-seitig sauber ab.
            await self.ws.send_json({"type": "done"})

    async def _respond(self, text: str, want_audio: bool) -> None:
        try:
            llm_task = asyncio.create_task(
                orchestrator_graph.ainvoke(
                    {"text": text, "tier": self.tier, "context_chunks": [], "response": ""}
                )
            )

            if want_audio and settings.filler_enabled:
                done, _ = await asyncio.wait({llm_task}, timeout=settings.filler_delay_ms / 1000)
                if not done:
                    await self._stream_filler()

            result = await llm_task
            response_text = result["response"]

            await self.ws.send_json({"type": "assistant_text", "text": response_text, "final": True})

            if want_audio:
                await self._stream_main_tts(response_text)
                await self.ws.send_json({"type": "audio_end"})

            await self.ws.send_json({"type": "done"})
        except asyncio.CancelledError:
            # interrupt/Barge-in: cancel_active verschickt das done.
            raise
        except Exception:
            logger.exception("Antwort-Pipeline fehlgeschlagen")
            with contextlib.suppress(Exception):
                await self.ws.send_json(
                    {"type": "error", "message": "interner Fehler bei der Antwortgenerierung"}
                )
                await self.ws.send_json({"type": "done"})

    async def _stream_filler(self) -> None:
        """Filler-Strategie 4.3: Piper-Audio ausspielen, waehrend das LLM noch
        rechnet. Fuer die App transparent Teil desselben audio_chunk-Streams,
        daher Resampling auf die Stream-Rate."""
        try:
            pcm, rate = await piper_client.synthesize(random.choice(_FILLER_PHRASES))
        except Exception:
            # Filler ist Komfort, kein Muss: Wenn Piper klemmt, wartet der
            # Nutzer einfach still auf die Hauptantwort.
            logger.warning("Filler-Synthese fehlgeschlagen - fahre ohne Filler fort")
            return

        pcm = resample_pcm16(pcm, rate, settings.target_sample_rate)
        for chunk in chunk_pcm(pcm):
            await self.ws.send_json(
                {
                    "type": "audio_chunk",
                    "audio": pcm_to_b64(chunk),
                    "sample_rate": settings.target_sample_rate,
                }
            )

    async def _stream_main_tts(self, text: str) -> None:
        async for rate, chunk in xtts_client.stream(text, self.voice_id):
            if rate != settings.target_sample_rate:
                chunk = resample_pcm16(chunk, rate, settings.target_sample_rate)
            await self.ws.send_json(
                {
                    "type": "audio_chunk",
                    "audio": pcm_to_b64(chunk),
                    "sample_rate": settings.target_sample_rate,
                }
            )


def _resolve_tier(websocket: WebSocket) -> int:
    # Tier kommt ueber das Login-Token (4.4); ohne/mit ungueltigem Token bis
    # 1.7b bewusst Fallback auf Tier 1 (Gast) statt eines harten Fehlers.
    token = websocket.query_params.get("token")
    if not token:
        return 1
    try:
        payload = decode_access_token(token)
        return int(payload.get("tier", 1))
    except Exception:
        return 1
