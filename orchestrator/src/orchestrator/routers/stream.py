import asyncio
import contextlib
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import repos
from ..audio import b64_to_pcm, chunk_pcm, pcm_to_b64, resample_pcm16
from ..config import settings
from ..graph import initial_state, orchestrator_graph
from ..schemas import DeviceTool
from ..security import decode_access_token
from ..services import filler_service
from ..services.stt_client import stt_client
from ..services.tool_executor import ToolExecutor
from ..services.tts_client import piper_client, xtts_client

logger = logging.getLogger(__name__)
router = APIRouter()


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
    User-Kontext aus dem Token (Tier, Charakter-Override, Standard-Stimme),
    Audio-Eingangspuffer und der laufende Antwort-Task (Barge-in)."""

    def __init__(self, websocket: WebSocket) -> None:
        self.ws = websocket
        self.mode = "chat"
        self.audio_buffer = bytearray()
        self.active_task: asyncio.Task | None = None
        # Geraete-Tool-Bridge (4.13): Manifest aus hello, laufende
        # tool_call-Roundtrips warten hier auf ihr tool_result.
        self.device_tools: list[DeviceTool] = []
        self.pending_tool_results: dict[str, asyncio.Future] = {}
        # Hoechstens ein Filler pro Turn (thinking ODER tool) - sonst
        # stapeln sich bei mehreren Tool-Calls die Phrasen.
        self._filler_played = False

        self.tier, self.username = _resolve_identity(websocket)
        self.system_prompt = repos.effective_system_prompt(self.username)
        self.voice_id = settings.default_voice_id
        if self.username:
            user = repos.get_user_by_username(self.username)
            if user and user["default_voice_id"]:
                self.voice_id = user["default_voice_id"]

    # ---- Frame-Dispatch ----------------------------------------------------

    async def handle(self, frame: dict) -> None:
        frame_type = frame.get("type")

        if frame_type == "hello":
            self.mode = frame.get("mode", "chat")
            if frame.get("voice_id"):
                self.voice_id = frame["voice_id"]
            self.device_tools = _parse_device_tools(frame.get("device_tools") or [])
            return

        if frame_type == "tool_result":
            # Antwort der App auf einen tool_call (4.13) - dem wartenden
            # Roundtrip zustellen. Unbekannte IDs (z. B. nach Barge-in)
            # verfallen kommentarlos.
            future = self.pending_tool_results.pop(str(frame.get("id")), None)
            if future is not None and not future.done():
                future.set_result(frame)
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
            self._filler_played = False
            executor = self._make_executor(want_audio)
            llm_task = asyncio.create_task(
                orchestrator_graph.ainvoke(
                    initial_state(text, self.tier, self.system_prompt, executor)
                )
            )

            if want_audio and settings.filler_enabled:
                done, _ = await asyncio.wait({llm_task}, timeout=settings.filler_delay_ms / 1000)
                if not done and not self._filler_played:
                    self._filler_played = True
                    await self._stream_filler(kind="thinking")

            result = await llm_task
            response_text = result["response"]

            await self.ws.send_json({"type": "assistant_text", "text": response_text, "final": True})

            if want_audio:
                await self._stream_main_tts(response_text)

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

    def _make_executor(self, want_audio: bool) -> ToolExecutor:
        """Session-gebundener Executor (1.12): Geraete-Tools aus dem
        hello-Manifest, Karten-Push und Tool-Filler haengen an DIESER
        Verbindung."""

        async def card_push(envelope: dict) -> None:
            # Frame-Format gegen docs/PROTOCOL.md im App-Repo verifizieren
            # (war aus dieser Umgebung nicht abrufbar).
            await self.ws.send_json({"type": "card", "card": envelope})

        async def on_tool_start(tool_name: str) -> None:
            # Tool-Trigger aus dem Admin-Panel (1.7d): "Ich schaue kurz in
            # den Kalender." - nur im Audio-Modus, hoechstens einmal pro Turn.
            if want_audio and settings.filler_enabled and not self._filler_played:
                self._filler_played = True
                await self._stream_filler(kind="tool", tool_name=tool_name)

        return ToolExecutor(
            device_tools=self.device_tools,
            device_call=self._call_device_tool,
            card_push=card_push,
            on_tool_start=on_tool_start,
        )

    async def _call_device_tool(self, name: str, arguments: dict) -> str:
        """Geraete-Tool-Roundtrip (4.13): tool_call an die App, auf das
        zugehoerige tool_result warten. Der Timeout ist grosszuegig, weil
        sensible Tools eine Bestaetigung des Nutzers erfordern koennen (4.4).
        Frame-Format gegen docs/PROTOCOL.md verifizieren."""
        call_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending_tool_results[call_id] = future
        try:
            await self.ws.send_json(
                {"type": "tool_call", "id": call_id, "name": name, "arguments": arguments}
            )
            frame = await asyncio.wait_for(future, timeout=settings.device_tool_timeout_s)
        except asyncio.TimeoutError:
            return f"Tool-Fehler: Geraet hat nicht innerhalb von {settings.device_tool_timeout_s}s geantwortet"
        finally:
            self.pending_tool_results.pop(call_id, None)

        result = frame.get("result", frame.get("error", ""))
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    async def _stream_filler(self, kind: str, tool_name: str | None = None) -> None:
        """Filler nach 4.3/4.14: bevorzugt vorgeneriertes XTTS-Audio in der
        Session-Stimme (1.7d), Fallback Piper-Live-Synthese mit dem
        Filler-Text. Fuer die App transparent Teil desselben
        audio_chunk-Streams, daher Resampling auf die Stream-Rate."""
        filler = filler_service.select_filler(kind, self.voice_id, tool_name)
        if filler is None:
            return

        try:
            if filler["path"] is not None:
                pcm, rate = filler_service.load_audio(filler["path"])
            else:
                pcm, rate = await piper_client.synthesize(filler["text"])
        except Exception:
            # Filler ist Komfort, kein Muss: Wenn er klemmt, wartet der
            # Nutzer einfach still auf die Hauptantwort.
            logger.warning("Filler-Audio fehlgeschlagen - fahre ohne Filler fort")
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
        try:
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
            await self.ws.send_json({"type": "audio_end"})
        except Exception:
            # TTS-Ausfall (z. B. Stimme ohne Sample) darf den Turn nicht
            # killen: Text ist schon raus, die App liest ihn laut
            # TTS-Fallback-Regel (4.13) selbst vor.
            logger.warning("Haupt-TTS fehlgeschlagen - Antwort bleibt Text-only")


def _parse_device_tools(raw: list) -> list[DeviceTool]:
    tools = []
    for entry in raw:
        try:
            tools.append(DeviceTool.model_validate(entry))
        except Exception:
            logger.warning("Ungueltiger Geraete-Tool-Eintrag im hello-Manifest: %r", entry)
    return tools


def _resolve_identity(websocket: WebSocket) -> tuple[int, str | None]:
    """(tier, username) aus dem Login-Token (4.4); ohne/mit ungueltigem
    Token bewusst Gast-Fallback statt eines harten Fehlers."""
    token = websocket.query_params.get("token")
    if not token:
        return 1, None
    try:
        payload = decode_access_token(token)
        return int(payload.get("tier", 1)), payload.get("sub")
    except Exception:
        return 1, None
