import asyncio
import contextlib
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import repos
from ..audio import Pcm16StreamConverter, b64_to_pcm, chunk_pcm, pcm_to_b64, resample_pcm16
from ..config import settings
from ..graph import initial_state, orchestrator_graph
from ..schemas import DeviceTool
from ..security import resolve_token
from ..services import filler_service, tts_engines
from ..services.litellm_client import litellm_client
from ..services.sentences import SentenceStream, split_sentences
from ..services.stt_client import stt_client
from ..services.tool_executor import ToolExecutor
from ..services.tts_client import piper_client

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/v1/assistant/stream")
async def assistant_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    # Auth wie bei REST (v1.13.1): Ein MITGESCHICKTES, aber ungueltiges
    # Token wird abgewiesen statt still zum Gast degradiert - sonst laufen
    # Dialoge scheinbar weiter, landen aber als unauffindbare
    # Gast-Gespraeche in der Historie. Ohne Token bleibt der Gast-Zugang
    # bewusst offen (Satelliten-Szenario, 4.4).
    tier, username, token_invalid = _resolve_identity(websocket)
    if token_invalid:
        await websocket.send_json({
            "type": "error",
            "message": "Token ungueltig oder abgelaufen - bitte neu anmelden",
        })
        await websocket.close(code=4401, reason="token invalid")
        return
    session = StreamSession(websocket, tier=tier, username=username)
    # session-Frame laut docs/PROTOCOL.md direkt nach dem Connect.
    await websocket.send_json({"type": "session", "session_id": session.session_id})
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

    def __init__(self, websocket: WebSocket, tier: int = 1, username: str | None = None) -> None:
        self.ws = websocket
        self.session_id = uuid.uuid4().hex
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
        # Zentrale Chat-Historie (Phase 2.5): das Gespraech wird lazy beim
        # ersten Input angelegt (keine leeren Gespraeche durch blosses
        # Verbinden); Karten des laufenden Turns sammeln sich fuer die
        # Persistenz der Assistant-Message.
        self.conversation_id: str | None = None
        self.device_name = ""
        self._turn_cards: list[dict] = []
        # Tool-/Agenten-Aufrufe des laufenden Turns (v1.12.1): live als
        # tool_activity-Frame an den Client (additiv - die Android-App
        # ignoriert unbekannte Frames) und am Turn-Ende in die Historie.
        self._turn_tools: list[dict] = []
        # Gestreamte Antwort und Filler teilen sich den audio_chunk-Stream
        # (v1.26): immer nur einer sendet, und dann am Stueck.
        self.audio_lock = asyncio.Lock()

        self.tier, self.username = tier, username
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
            # Manifest-Feld laut docs/PROTOCOL.md: "tools".
            self.device_tools = _parse_device_tools(frame.get("tools") or [])
            self.device_name = str((frame.get("device") or {}).get("name") or "")
            # Historie fortsetzen (Protokoll-Erweiterung 2.5): optionales
            # conversation_id im hello. Nur eigene Gespraeche - Gaeste
            # ohne Login koennen keine Fortsetzung beanspruchen.
            requested = frame.get("conversation_id")
            if requested and self.username:
                conversation = repos.get_conversation(str(requested))
                if conversation and conversation["username"] == self.username:
                    self.conversation_id = conversation["id"]
                    await self._send_conversation_frame()
            return

        if frame_type == "tool_result":
            # Antwort der App auf einen tool_call (4.13) - dem wartenden
            # Roundtrip zustellen. Unbekannte IDs (z. B. nach Barge-in)
            # verfallen kommentarlos.
            future = self.pending_tool_results.pop(str(frame.get("call_id")), None)
            if future is not None and not future.done():
                future.set_result(frame)
            return

        if frame_type == "text_input":
            # Antwort-Modalitaet: Text rein -> Text raus. Sprachausgabe gibt
            # es nur fuer gesprochene Eingaben (siehe finish_audio_input) -
            # so liest die Assistenz nicht ungefragt getippte Chats vor.
            await self.start_response(frame.get("text", ""), want_audio=False)
            return

        if frame_type == "audio_chunk":
            pcm = b64_to_pcm(frame.get("data", ""))
            if len(self.audio_buffer) + len(pcm) > settings.max_audio_buffer_bytes:
                self.audio_buffer.clear()
                await self.ws.send_json({"type": "error", "message": "Audio-Puffer-Limit ueberschritten"})
                return
            self.audio_buffer.extend(pcm)
            return

        if frame_type == "audio_end":
            await self.finish_audio_input()
            return

        if frame_type == "image_input":
            # Screenshot-/Bild-Analyse (Phase 2.5/3): Base64-Bild + optionale
            # Frage. speak=true fuer sprachgetriebene Flows (Antwort kommt
            # dann wie bei Audio-Eingaben auch als TTS-Stream).
            data = frame.get("data", "")
            if not data:
                await self.ws.send_json({"type": "error", "message": "image_input ohne Bilddaten"})
                return
            if len(data) > settings.max_image_b64_bytes:
                await self.ws.send_json({"type": "error", "message": "Bild zu gross"})
                return
            mime = frame.get("mime") or "image/png"
            await self.start_response(
                frame.get("text", ""),
                want_audio=bool(frame.get("speak")),
                image_data_url=f"data:{mime};base64,{data}",
            )
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

        # Gesprochene Frage -> gesprochene Antwort (+ Details im Chat).
        await self.start_response(text, want_audio=True)

    # ---- Zentrale Chat-Historie (Phase 2.5) ---------------------------------

    async def _send_conversation_frame(self) -> None:
        # Teilt dem Client die Gespraechs-ID mit (neu angelegt oder im hello
        # bestaetigt) - damit kann er den Turn spaeter ueber
        # GET /v1/conversations/{id} wiederfinden bzw. fortsetzen.
        await self.ws.send_json(
            {"type": "conversation", "conversation_id": self.conversation_id}
        )

    async def _ensure_conversation(self, first_text: str) -> None:
        if self.conversation_id is not None:
            return
        conversation = repos.create_conversation(
            self.username, self.device_name, title=first_text.strip() or "Neues Gespraech"
        )
        self.conversation_id = conversation["id"]
        await self._send_conversation_frame()

    async def _persist_user_message(self, text: str, has_image: bool) -> None:
        # Persistenz ist Komfort, kein Muss: ein DB-Fehler darf den Turn
        # nicht killen (gleiches Prinzip wie Filler/TTS-Ausfaelle).
        try:
            await self._ensure_conversation(text if not has_image else (text or "Bildanfrage"))
            content = text.strip() or ("[Bild]" if has_image else "")
            repos.append_message(self.conversation_id, "user", content, has_image=has_image)
        except Exception:
            logger.exception("Konnte User-Message nicht persistieren")

    def _conversation_history(self) -> list[dict]:
        """Letzte Turns der Konversation als LLM-Messages (v1.13).
        Karten des Turns werden mit ihren DATEN (ohne Layout-HTML) an die
        Assistant-Message angehaengt - so kennt das LLM 'die Karte von
        eben' und kann kontextbezogen weitermachen. Verlauf ist Komfort:
        ein Ladefehler darf den Turn nicht kosten."""
        if self.conversation_id is None:
            return []
        try:
            messages = repos.list_messages(self.conversation_id)
        except Exception:
            logger.exception("Konnte Gespraechsverlauf nicht laden")
            return []
        history: list[dict] = []
        for message in messages[-settings.history_max_messages:]:
            content = (message["content"] or "").strip()
            for card in message.get("cards") or []:
                # Layout-HTML ist Kontext-Ballast; die Werte stecken in den
                # restlichen data-Feldern (code-card bekommt sie ja auch so).
                data = {k: v for k, v in (card.get("data") or {}).items() if k != "html"}
                label = card.get("title") or card.get("type", "Karte")
                note = f"[Karte angezeigt: {label}"
                if data:
                    note += f" - Daten: {json.dumps(data, ensure_ascii=False)}"
                note += "]"
                content = f"{content}\n{note}".strip()
            if not content:
                continue
            if len(content) > settings.history_max_chars_per_message:
                content = content[: settings.history_max_chars_per_message] + " …"
            history.append({"role": message["role"], "content": content})
        return history

    async def _persist_assistant_message(self, text: str) -> None:
        if self.conversation_id is None:
            return
        try:
            repos.append_message(
                self.conversation_id, "assistant", text,
                cards=self._turn_cards or None, tools=self._turn_tools or None,
            )
        except Exception:
            logger.exception("Konnte Assistant-Message nicht persistieren")

    # ---- Antwort-Pipeline --------------------------------------------------

    async def start_response(
        self, text: str, want_audio: bool, image_data_url: str = ""
    ) -> None:
        # Neuer Input waehrend eine Antwort laeuft = implizites Barge-in.
        await self.cancel_active(notify=False)
        self.active_task = asyncio.create_task(
            self._respond(text, want_audio, image_data_url)
        )

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

    async def _respond(self, text: str, want_audio: bool, image_data_url: str = "") -> None:
        streamer: TurnStreamer | None = None
        try:
            self._filler_played = False
            self._turn_cards = []
            self._turn_tools = []
            # Gespraechsgedaechtnis (v1.13) VOR dem Persistieren des
            # aktuellen Turns laden - sonst stuende die aktuelle Frage
            # doppelt im Kontext.
            history = self._conversation_history()
            await self._persist_user_message(text, has_image=bool(image_data_url))
            executor = self._make_executor(want_audio)
            # LLM-Streaming (v1.26, Schalter im Panel): Text-Deltas sofort in
            # den Chat, bei Sprach-Turns Satz fuer Satz in die Sprachausgabe.
            if repos.llm_streaming_enabled():
                streamer = TurnStreamer(self, want_audio)
            llm_task = asyncio.create_task(
                orchestrator_graph.ainvoke(
                    initial_state(
                        text, self.tier, self.system_prompt, executor, image_data_url,
                        # Voice-First (v1.13): gesprochene Frage -> das LLM
                        # antwortet direkt kurz, Umfangreiches als Karte.
                        voice_mode=want_audio,
                        history=history,
                        streamer=streamer,
                    )
                )
            )

            if want_audio and settings.filler_enabled:
                # Filler ZUERST waehlen - sein delay_ms bestimmt, wie lange
                # die Antwort Zeit hat, bevor er spielen darf. Schnelle
                # Antworten blockiert so kein Filler mehr. Beim Streaming
                # zaehlt schon der erste fertige Satz als Antwort.
                if streamer is None:
                    await self._race_filler(llm_task, kind="thinking")
                else:
                    first = asyncio.create_task(streamer.first_answer(llm_task))
                    try:
                        await self._race_filler(first, kind="thinking")
                    finally:
                        first.cancel()
            if streamer is not None and want_audio:
                # Erst nach dem Filler-Rennen: Filler und Antwort duerfen
                # sich im audio_chunk-Stream nicht abwechseln.
                streamer.start_speaking()

            result = await llm_task
            response_text = result["response"]

            await self.ws.send_json({"type": "assistant_text", "text": response_text, "final": True})
            await self._persist_assistant_message(response_text)

            if want_audio and streamer is not None:
                await streamer.finish(response_text)
            elif want_audio:
                # Kurze Sprachantwort, Details im Chat: lange Antworten
                # werden fuer die Sprachausgabe zusammengefasst (Schalter im
                # Panel), der volle Text steht bereits als assistant_text im
                # Chat.
                await self._stream_main_tts(await self._speech_text(response_text))

            await self.ws.send_json({"type": "done"})
        except asyncio.CancelledError:
            # interrupt/Barge-in: cancel_active verschickt das done.
            raise
        except Exception as exc:
            logger.exception("Antwort-Pipeline fehlgeschlagen")
            # Die echte Ursache mitschicken statt sie zu verschlucken - im
            # Heim-Setup ist der Nutzer der Admin, die Diagnose direkt am
            # Geraet spart den Umweg ueber docker logs.
            detail = f"{type(exc).__name__}: {str(exc)[:250]}"
            with contextlib.suppress(Exception):
                await self.ws.send_json(
                    {
                        "type": "error",
                        "message": f"interner Fehler bei der Antwortgenerierung ({detail})",
                    }
                )
                await self.ws.send_json({"type": "done"})
        finally:
            if streamer is not None:
                # Barge-in oder Fehler: die Sprachausgabe endet mit dem Turn.
                streamer.cancel()

    def _make_executor(self, want_audio: bool) -> ToolExecutor:
        """Session-gebundener Executor (1.12): Geraete-Tools aus dem
        hello-Manifest, Karten-Push und Tool-Filler haengen an DIESER
        Verbindung."""

        async def card_push(envelope: dict) -> None:
            # Frame-Format laut docs/PROTOCOL.md (App-Repo): {type, card}.
            # Karten des Turns wandern zusaetzlich in die Historie, damit
            # sie beim spaeteren Oeffnen des Gespraechs wieder erscheinen.
            self._turn_cards.append(envelope)
            await self.ws.send_json({"type": "card", "card": envelope})

        async def on_tool_start(tool_name: str, pending: asyncio.Task) -> None:
            # Tool-Trigger aus dem Admin-Panel (1.7d): "Ich schaue kurz in
            # den Kalender." - nur im Audio-Modus, hoechstens einmal pro
            # Turn. Das delay_ms des Fillers laesst schnellen Tools den
            # Vortritt: Ist das Tool vorher fertig, entfaellt der Filler.
            if want_audio and settings.filler_enabled:
                await self._race_filler(pending, kind="tool", tool_name=tool_name)

        async def on_activity(tool_name: str, status: str) -> None:
            # "Was tut die KI gerade?" fuer Chat- UND Voice-Verlauf: Live-
            # Frame an den Client, Endzustand in die Turn-Liste fuer die
            # Historie. running legt einen Eintrag an, done/error stempelt
            # den letzten laufenden Eintrag desselben Tools.
            if status == "running":
                self._turn_tools.append({"tool": tool_name, "status": "running"})
            else:
                for entry in reversed(self._turn_tools):
                    if entry["tool"] == tool_name and entry["status"] == "running":
                        entry["status"] = status
                        break
            await self.ws.send_json(
                {"type": "tool_activity", "tool": tool_name, "status": status}
            )

        return ToolExecutor(
            device_tools=self.device_tools,
            device_call=self._call_device_tool,
            card_push=card_push,
            on_tool_start=on_tool_start,
            on_activity=on_activity,
        )

    async def _call_device_tool(self, name: str, arguments: dict) -> str:
        """Geraete-Tool-Roundtrip (4.13): tool_call an die App, auf das
        zugehoerige tool_result warten. Der Timeout ist grosszuegig, weil
        sensible Tools eine Bestaetigung des Nutzers erfordern koennen (4.4).
        Frame-Felder laut docs/PROTOCOL.md: call_id + ok + result."""
        call_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending_tool_results[call_id] = future
        try:
            await self.ws.send_json(
                {"type": "tool_call", "call_id": call_id, "name": name, "arguments": arguments}
            )
            frame = await asyncio.wait_for(future, timeout=settings.device_tool_timeout_s)
        except asyncio.TimeoutError:
            return f"Tool-Fehler: Geraet hat nicht innerhalb von {settings.device_tool_timeout_s}s geantwortet"
        finally:
            self.pending_tool_results.pop(call_id, None)

        result = frame.get("result", "")
        result_text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        if frame.get("ok") is False:
            return f"Tool-Fehler auf dem Geraet: {result_text or 'keine Details'}"
        return result_text

    async def _speech_text(self, response_text: str) -> str:
        """Kurze Sprachfassung fuer lange Antworten (zweiter, kleiner
        LLM-Call; Schalter und Limit im Panel, v1.26). Schlaegt er fehl,
        wird eben der volle Text gesprochen - Komfort-Feature, kein Muss."""
        if not repos.voice_summary_enabled() or len(response_text) <= repos.voice_summary_max_chars():
            return response_text
        return await self._summarize(response_text) or response_text

    async def _summarize(self, response_text: str, already_spoken: str = "") -> str:
        """Kurzfassung per LLM; leer bei Fehler. already_spoken (Streaming,
        v1.26): Der Anfang lief schon live - zusammengefasst wird nur der
        Rest, ohne das Gesagte zu wiederholen."""
        # Prompt kommt aus Admin > Charakter (app_settings) - dort z. B.
        # auch Tonalitaet der Kurzfassung steuerbar.
        system_prompt = repos.voice_summary_prompt()
        if already_spoken:
            system_prompt += "\n\n" + _CONTINUATION_PROMPT.format(spoken=already_spoken)
        try:
            summary = await litellm_client.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": response_text},
                ]
            )
            return summary.strip()
        except Exception:
            logger.warning("Sprach-Kurzfassung fehlgeschlagen - spreche den vollen Text")
            return ""

    async def _race_filler(self, pending: asyncio.Task, kind: str, tool_name: str | None = None) -> None:
        """Filler gegen die laufende Arbeit rennen lassen: Der ausgewaehlte
        Filler bestimmt per delay_ms selbst, wie lange gewartet wird, bevor
        er spielen darf. Ist die Arbeit (LLM-Antwort bzw. Tool) vorher
        fertig, entfaellt er - schnelle Antworten werden nicht blockiert.
        delay_ms = 0 heisst: sofort spielen."""
        if self._filler_played:
            return
        try:
            filler = filler_service.select_filler(kind, self.voice_id, tool_name)
            if filler is None:
                return

            delay_ms = filler.get("delay_ms")
            if delay_ms is None:
                delay_ms = settings.filler_delay_ms
            if delay_ms > 0:
                done, _ = await asyncio.wait({pending}, timeout=delay_ms / 1000)
                if done or self._filler_played:
                    return

            self._filler_played = True
            await self._play_filler(filler)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Filler ist Komfort, kein Muss - er darf den Turn unter keinen
            # Umstaenden zum "internen Fehler" machen.
            logger.exception("Filler-Pfad fehlgeschlagen - fahre ohne Filler fort")

    async def _play_filler(self, filler: dict) -> None:
        """Filler nach 4.3/4.14: bevorzugt vorgeneriertes XTTS-Audio in der
        Session-Stimme (1.7d), Fallback Piper-Live-Synthese mit dem
        Filler-Text. Fuer die App transparent Teil desselben
        audio_chunk-Streams, daher Resampling auf die Stream-Rate."""
        try:
            if filler["path"] is not None:
                pcm, rate = filler_service.load_audio(filler["path"])
            else:
                pcm, rate = await piper_client.synthesize(filler["text"])
        except Exception as exc:
            # Filler ist Komfort, kein Muss: Wenn er klemmt, wartet der
            # Nutzer einfach still auf die Hauptantwort.
            logger.warning("Filler-Audio fehlgeschlagen - fahre ohne Filler fort: %s", str(exc)[:200])
            return

        pcm = resample_pcm16(pcm, rate, settings.target_sample_rate)
        # Ein Tool-Filler kann mitten in die gestreamte Antwort fallen
        # (v1.26) - die Sperre haelt Saetze und Filler am Stueck.
        async with self.audio_lock:
            for chunk in chunk_pcm(pcm):
                await self.ws.send_json(
                    {
                        "type": "audio_chunk",
                        "data": pcm_to_b64(chunk),
                        "sample_rate": settings.target_sample_rate,
                    }
                )

    async def _stream_main_tts(self, text: str) -> None:
        try:
            # Engine der Hauptstimme waehlt der Admin im Panel (v1.17);
            # faellt eine Test-Engine vor dem ersten Chunk aus, spricht XTTS.
            # Umrechnen mit Zustand ueber den ganzen Stream (v1.23): einzeln
            # umgerechnete Chunks knacken an jeder Grenze.
            converter: Pcm16StreamConverter | None = None
            async for rate, chunk in tts_engines.stream_main(text, self.voice_id):
                if converter is None or converter.src_rate != rate:
                    converter = Pcm16StreamConverter(rate, settings.target_sample_rate)
                chunk = converter.convert(chunk)
                if not chunk:
                    continue
                await self.ws.send_json(
                    {
                        "type": "audio_chunk",
                        "data": pcm_to_b64(chunk),
                        "sample_rate": settings.target_sample_rate,
                    }
                )
            await self.ws.send_json({"type": "audio_end"})
        except Exception:
            # TTS-Ausfall (z. B. Stimme ohne Sample) darf den Turn nicht
            # killen: Text ist schon raus, die App liest ihn laut
            # TTS-Fallback-Regel (4.13) selbst vor.
            logger.exception("Haupt-TTS fehlgeschlagen - Antwort bleibt Text-only")


# Fortsetzung der Kurzfassung beim Streaming (v1.26): Der Anfang der Antwort
# lief schon live, das Limit war erreicht - der Rest kommt als Kurzfassung.
_CONTINUATION_PROMPT = (
    "Der Anfang der Antwort wurde bereits vorgelesen: \"{spoken}\" "
    "Fasse nur den restlichen Inhalt kurz zusammen, wiederhole nichts davon "
    "und schliesse nahtlos an das Gesagte an."
)


class TurnStreamer:
    """LLM-Streaming eines Turns (v1.26). Bekommt die Text-Deltas aus dem
    Graphen: Jedes Delta geht als assistant_text (final=false) an den
    Client; bei Sprach-Turns schneidet ein SentenceStream daraus Saetze,
    und ein Worker spricht sie der Reihe nach, waehrend das LLM weiter
    schreibt.

    Kurzfassung (Schalter im Panel): Live gesprochen wird bis zum Limit;
    wuerde ein Satz es reissen, endet die Live-Ausgabe, und nach der
    Antwort spricht die Kurzfassung des Rests. Ohne Kurzfassung wird
    alles gesprochen. Die Engine waehlt der erste Satz (mit Rueckfall),
    alle weiteren bleiben bei ihr; scheitert ein spaeterer Satz, endet die
    Sprachausgabe des Turns - der Text steht ja im Chat."""

    def __init__(self, session: "StreamSession", want_audio: bool) -> None:
        self.session = session
        self.want_audio = want_audio
        self.sentences = SentenceStream()
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.first_sentence = asyncio.Event()
        self.limit = repos.voice_summary_max_chars() if repos.voice_summary_enabled() else None
        self.spoken: list[str] = []
        self.skipped: list[str] = []
        self.engine_id: str | None = None
        self.converter: Pcm16StreamConverter | None = None
        self.audio_sent = False
        self.failed = False
        self.worker: asyncio.Task | None = None

    # ---- Seite des LLM (Graph) ------------------------------------------

    async def delta(self, text: str) -> None:
        await self.session.ws.send_json({"type": "assistant_text", "text": text, "final": False})
        if self.want_audio:
            self._enqueue(self.sentences.feed(text))

    async def round_end(self) -> None:
        # Ende einer LLM-Runde: Text vor einem Tool-Call ("Ich schaue kurz
        # nach") hat oft kein Satzzeichen - trotzdem jetzt sprechen.
        if self.want_audio:
            self._enqueue(self.sentences.flush())

    def _enqueue(self, pieces: list[str]) -> None:
        for piece in pieces:
            self.queue.put_nowait(piece)
            self.first_sentence.set()

    async def first_answer(self, llm_task: asyncio.Task) -> None:
        """Fertig, sobald der erste Satz bereitsteht oder das LLM fertig
        ist - das Gegenstueck zum Filler-Rennen."""
        waiter = asyncio.create_task(self.first_sentence.wait())
        try:
            await asyncio.wait({waiter, llm_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()

    # ---- Sprachausgabe ----------------------------------------------------

    def start_speaking(self) -> None:
        self.worker = asyncio.create_task(self._speak_queue())

    async def finish(self, response_text: str) -> None:
        """Nach der Antwort: Rest der Queue sprechen, bei erreichtem Limit
        die Kurzfassung des Rests, dann audio_end."""
        self.queue.put_nowait(None)
        if self.worker is not None:
            await self.worker
        if self.skipped and not self.failed:
            already = " ".join(self.spoken)
            rest = await self.session._summarize(response_text, already_spoken=already)
            for piece in split_sentences(rest) if rest else self.skipped:
                await self._speak(piece)
                if self.failed:
                    break
        if self.audio_sent:
            await self.session.ws.send_json({"type": "audio_end"})

    def cancel(self) -> None:
        if self.worker is not None and not self.worker.done():
            self.worker.cancel()

    async def _speak_queue(self) -> None:
        while (piece := await self.queue.get()) is not None:
            if self.failed:
                continue
            used = sum(len(p) + 1 for p in self.spoken)
            if self.skipped or (self.limit is not None and used + len(piece) > self.limit):
                # Limit erreicht: ab hier nichts mehr live, der Rest kommt
                # nach der Antwort als Kurzfassung.
                self.skipped.append(piece)
                continue
            await self._speak(piece)

    async def _speak(self, text: str) -> None:
        def chosen(engine_id: str) -> None:
            self.engine_id = engine_id

        try:
            async with self.session.audio_lock:
                stream = tts_engines.stream_main(
                    text, self.session.voice_id, engine_id=self.engine_id, on_engine=chosen
                )
                async for rate, chunk in stream:
                    # Umrechnen mit Zustand ueber alle Saetze (v1.23).
                    if self.converter is None or self.converter.src_rate != rate:
                        self.converter = Pcm16StreamConverter(rate, settings.target_sample_rate)
                    chunk = self.converter.convert(chunk)
                    if not chunk:
                        continue
                    if not self.audio_sent:
                        # Die Antwort spricht - ein Tool-Filler waere jetzt
                        # nur noch Stoerung.
                        self.audio_sent = True
                        self.session._filler_played = True
                    await self.session.ws.send_json(
                        {
                            "type": "audio_chunk",
                            "data": pcm_to_b64(chunk),
                            "sample_rate": settings.target_sample_rate,
                        }
                    )
            self.spoken.append(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Wie im klassischen Pfad: TTS-Ausfall kostet nur die Stimme,
            # der Text steht im Chat (bzw. die App liest ihn vor, 4.13).
            logger.exception("Haupt-TTS fehlgeschlagen - Rest der Antwort bleibt Text-only")
            self.failed = True


def _parse_device_tools(raw: list) -> list[DeviceTool]:
    tools = []
    for entry in raw:
        try:
            tools.append(DeviceTool.model_validate(entry))
        except Exception:
            logger.warning("Ungueltiger Geraete-Tool-Eintrag im hello-Manifest: %r", entry)
    return tools


def _resolve_identity(websocket: WebSocket) -> tuple[int, str | None, bool]:
    """(tier, username, token_invalid) aus dem Login-Token (4.4). OHNE
    Token: Gast (Satelliten-Szenario). MIT ungueltigem Token: Kennzeichen
    fuer den Handler - der weist die Verbindung ab, wie REST mit 401
    (v1.13.1, vorher stiller Gast-Fallback).

    Die App sendet den Token laut docs/PROTOCOL.md als
    Authorization-Header; der Query-Parameter bleibt als Fallback fuer
    die Web-UI und manuelle Test-Skripte (Browser-WebSockets koennen
    keine Header setzen)."""
    token = None
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
    if not token:
        token = websocket.query_params.get("token")
    if not token:
        return 1, None, False
    payload = resolve_token(token)
    if payload is None:
        return 1, None, True
    return int(payload.get("tier", 1)), payload.get("sub"), False
