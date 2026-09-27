"""audio.cpp als Anbieter fuer Sprachausgabe (v1.21) - angebunden wie LiteLLM.

audio.cpp (github.com/0xShug0/audio.cpp) ist EIN Server fuer viele lokale
Audio-Modelle (TTS mit und ohne Klonen, ASR, ...) mit OpenAI-aehnlicher
API. Wie beim LLM ueber LiteLLM fragt der Orchestrator nur die Liste ab
(GET /v1/models) und macht jedes TTS-Modell zu einer Engine
"audiocpp:<Modell-ID>" - Aktivieren, Probehoeren und Filler funktionieren
damit wie bei jeder anderen Engine, ohne eigenen Container pro Modell.

Laden und Entladen: audio.cpp laedt ein Modell beim ersten Request (lazy)
und behaelt es; POST /v1/tasks/unload_models gibt es wieder frei (geht auch
ohne die WebUI-Verwaltung). Auf welcher Karte audio.cpp rechnet, legt seine
eigene Konfiguration fest (device) - der Orchestrator kann das nicht
umstellen, nur wissen (Angabe im Panel), um beim Aktivieren andere
Sprachausgaben auf derselben Karte zu entladen.

Hier steht nur der Draht zu audio.cpp; Registry, Status-Texte und das
Aktivieren liegen in tts_engines."""

import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from .. import repos
from ..audio import chunk_pcm, wav_to_pcm16
from ..config import settings
from .sentences import split_sentences
from .tts_client import reference_wav

logger = logging.getLogger(__name__)

ENGINE_PREFIX = "audiocpp:"
# app_settings: Adresse (leer = .env), Karte laut Admin, zuletzt gesehene
# TTS-Modelle (JSON) und die gewaehlte eingebaute Stimme je Modell (JSON).
URL_SETTING = "audiocpp_base_url"
DEVICE_SETTING = "audiocpp_device"
MODELS_SETTING = "audiocpp_models"
VOICES_SETTING = "audiocpp_voices"

STATUS_TIMEOUT_S = 3.0
# Live-Turn: Ist das Modell belegt (z. B. Filler-Generierung), nicht lange
# anstehen - dann spricht die Rueckfallebene.
LIVE_BUSY_TIMEOUT_MS = 8000
# Ein fehlgeschlagenes Laden (z. B. VRAM voll) nicht bei jedem Turn neu
# anstossen.
WARM_UP_RETRY_S = 60.0
WARM_UP_TEXT = "Hallo, ich bin bereit."

# Qwen3-TTS kennt Sprachen nur beim Namen ("german"); die anderen Familien
# nehmen ISO-Codes. Unbekanntes bleibt leer = das Modell erkennt selbst.
_QWEN3_LANGUAGES = {
    "de": "german", "en": "english", "zh": "chinese", "ja": "japanese", "ko": "korean",
    "fr": "french", "ru": "russian", "pt": "portuguese", "es": "spanish", "it": "italian",
}


class AudioCppError(RuntimeError):
    """Antwort von audio.cpp mit HTTP-Fehler (Text = seine Meldung)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"{message} (HTTP {status_code})" if status_code else message)
        self.status_code = status_code
        self.message = message


class AudioCppNotReady(AudioCppError):
    """Modell (noch) nicht geladen - der Live-Turn wartet nicht darauf."""


# ---- Adresse, Karte, Modell-Liste -------------------------------------------------


def base_url() -> str:
    """Adresse des audio.cpp-Servers: im Panel gesetzt, sonst .env; leer =
    nicht eingerichtet."""
    return (repos.get_setting(URL_SETTING) or settings.audiocpp_base_url or "").rstrip("/")


def device() -> str:
    """Karte, auf der audio.cpp laut Admin rechnet ("cuda:1", "cpu" oder
    "" = nicht angegeben)."""
    return repos.get_setting(DEVICE_SETTING) or ""


def engine_id(model: str) -> str:
    return ENGINE_PREFIX + model


def model_of(engine: str) -> str | None:
    return engine[len(ENGINE_PREFIX):] if engine.startswith(ENGINE_PREFIX) else None


def cached_models() -> list[dict]:
    """TTS-Modelle der letzten erfolgreichen Abfrage - damit die Registry
    ohne Netzwerk auskommt und die Liste einen Neustart uebersteht."""
    try:
        models = json.loads(repos.get_setting(MODELS_SETTING) or "[]")
    except ValueError:
        return []
    return [m for m in models if isinstance(m, dict) and m.get("id")] if isinstance(models, list) else []


def _store_models(models: list[dict]) -> None:
    tts = [{"id": str(m["id"]), "family": str(m.get("family") or ""), "mode": str(m.get("mode") or "")}
           for m in models if m.get("task") == "tts" and m.get("id")]
    encoded = json.dumps(tts)
    if repos.get_setting(MODELS_SETTING) != encoded:
        repos.set_setting(MODELS_SETTING, encoded)


async def fetch_server() -> dict:
    """/health und /v1/models in einem Rutsch; Erfolg aktualisiert die
    gemerkte TTS-Liste. Ist der Server weg, bleibt die alte Liste stehen -
    ein kurz gestoppter Server soll die Auswahl im Panel nicht leeren.
    Verbindungsfehler gehen an den Aufrufer (der uebersetzt sie)."""
    async with httpx.AsyncClient(base_url=base_url(), timeout=STATUS_TIMEOUT_S) as client:
        # Beide Antworten abwarten, auch wenn eine scheitert - sonst liefe
        # die andere noch, waehrend der Client schon schliesst.
        health, listing = await asyncio.gather(
            client.get("/health"), client.get("/v1/models"), return_exceptions=True)
    if isinstance(listing, BaseException):
        raise listing
    listing.raise_for_status()
    models = listing.json().get("data", [])
    if not isinstance(models, list):
        raise ValueError("/v1/models liefert keine Liste")
    models = [m for m in models if isinstance(m, dict) and m.get("id")]
    _store_models(models)
    try:
        health_data = health.json() if not isinstance(health, BaseException) and health.status_code == 200 else {}
    except ValueError:
        health_data = {}
    return {"health": health_data if isinstance(health_data, dict) else {}, "models": models}


def forget_models() -> None:
    """Gemerkte Liste verwerfen (neue Adresse = anderer Server)."""
    repos.set_setting(MODELS_SETTING, "[]")


async def list_voices(model: str) -> list[str]:
    """Eingebaute Stimmen, Presets und Stimmen-Bibliothek eines Modells
    (GET /v1/audio/voices) - fuer die Auswahl im Panel."""
    async with httpx.AsyncClient(base_url=base_url(), timeout=STATUS_TIMEOUT_S) as client:
        response = await client.get("/v1/audio/voices", params={"model": model})
    response.raise_for_status()
    voices = response.json().get("voices", [])
    return [str(v) for v in voices] if isinstance(voices, list) else []


def voice_choices() -> dict[str, str]:
    """Modell -> gewaehlte eingebaute Stimme. Fehlt ein Modell, klont es aus
    dem Voice-Sample der jeweiligen Stimme (Standard)."""
    try:
        data = json.loads(repos.get_setting(VOICES_SETTING) or "{}")
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items() if v} if isinstance(data, dict) else {}


def set_voice_choice(model: str, voice: str) -> None:
    choices = voice_choices()
    if voice:
        choices[model] = voice
    else:
        choices.pop(model, None)
    repos.set_setting(VOICES_SETTING, json.dumps(choices))


async def unload(models: list[str]) -> list[str]:
    """Modelle entladen (VRAM frei); audio.cpp laedt sie beim naechsten
    Request von selbst wieder. Wartet eine laufende Synthese ab."""
    if not models:
        return []
    async with httpx.AsyncClient(base_url=base_url(), timeout=60.0) as client:
        response = await client.post("/v1/tasks/unload_models", json={"model_ids": models})
    if response.status_code >= 400:
        raise AudioCppError(response.status_code, _error_message(response))
    return [str(m) for m in response.json().get("unloaded", [])]


# ---- Laden ("aufwaermen") ---------------------------------------------------------
#
# Ohne WebUI-Verwaltung kennt audio.cpp kein "nur laden": Ein kurzer Satz
# laedt das Modell (und legt nebenbei die Stimme in seinen Referenz-Cache),
# das Audio wird verworfen.

_warmups: dict[str, asyncio.Task] = {}
_warm_up_errors: dict[str, tuple[float, str]] = {}


def warming_up(model: str) -> bool:
    task = _warmups.get(model)
    return task is not None and not task.done()


def warm_up_error(model: str) -> str | None:
    entry = _warm_up_errors.get(model)
    return entry[1] if entry else None


def start_warm_up(model: str, voice_id: str | None, force: bool = False) -> asyncio.Task | None:
    """Laden im Hintergrund anstossen (laeuft schon eins, wird es geteilt).
    Nach einem Fehlschlag erst wieder nach WARM_UP_RETRY_S - ausser force
    (Aktivieren im Panel)."""
    task = _warmups.get(model)
    if task is not None and not task.done():
        return task
    failed = _warm_up_errors.get(model)
    if not force and failed and time.monotonic() - failed[0] < WARM_UP_RETRY_S:
        return None
    task = asyncio.create_task(_warm_up(model, voice_id))
    _warmups[model] = task
    return task


async def ensure_loaded(model: str, voice_id: str | None, timeout: float) -> bool:
    """Fuers Aktivieren: laden und bis zu `timeout` s darauf warten. True =
    fertig, False = laedt noch (im Hintergrund weiter). Ein Fehler beim
    Testsatz wird geworfen."""
    task = start_warm_up(model, voice_id, force=True)
    try:
        error = await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        return False
    if error is not None:
        raise error
    return True


async def _warm_up(model: str, voice_id: str | None) -> Exception | None:
    # Gibt den Fehler zurueck statt ihn zu werfen: Niemand muss auf die
    # Hintergrund-Aufgabe warten, und asyncio soll nicht "Task exception was
    # never retrieved" melden.
    try:
        async for _ in AudioCppClient(model).stream(WARM_UP_TEXT, voice_id or "", wait_for_load=True):
            pass
    except Exception as exc:
        logger.warning("audio.cpp: Laden von '%s' fehlgeschlagen: %s", model, exc)
        _warm_up_errors[model] = (time.monotonic(), str(exc)[:300] or type(exc).__name__)
        return exc
    _warm_up_errors.pop(model, None)
    return None


def warm_up_voice() -> str | None:
    """Stimme fuers Laden beim Aktivieren: die Standardstimme, sonst die
    erste mit Voice-Sample - klonende Modelle brauchen eins."""
    candidates = [settings.default_voice_id] + [v["id"] for v in repos.list_voices()]
    for voice_id in candidates:
        if (Path(settings.voices_dir) / f"{voice_id}.wav").exists():
            return voice_id
    return settings.default_voice_id


# ---- Sprachausgabe ------------------------------------------------------------------


def language_hint(family: str, language: str | None) -> str | None:
    code = (language or "").strip().lower().replace("_", "-").split("-")[0]
    if not code:
        return None
    if family == "qwen3_tts":
        return _QWEN3_LANGUAGES.get(code)
    return code


# Die Familien unterscheiden sich (Transkript ja/nein, Sprachcodes), und
# audio.cpp verraet das nicht vorab: Lehnt ein Modell ein Feld ab, fragt der
# Client einmal ohne nach und merkt sich das (pro Server und Modell).
_dropped: dict[tuple[str, str], set[str]] = {}
# Voice-Sample als Base64 - nicht fuer jeden Satz neu lesen und umrechnen.
_references: dict[str, tuple[tuple[int, int], str]] = {}


def _rejected_fields(message: str, payload: dict) -> set[str]:
    text = message.lower()
    fields: set[str] = set()
    if "reference_text" in payload and "request option: reference_text" in text:
        fields.add("reference_text")
    if "language" in payload and "language" in text:
        fields.add("language")
    return fields


def _reference(voice_id: str) -> str | None:
    path = Path(settings.voices_dir) / f"{voice_id}.wav"
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _references.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    data = base64.b64encode(reference_wav(path.read_bytes())).decode("ascii")
    _references[str(path)] = (key, data)
    return data


# audio.cpp baut beim ERSTEN Satz eines Modells Caches fuer konstante Tensoren
# und laedt sie auf die Karte. Scheitert das (meist: VRAM voll), bleibt der
# Cache halb gefuellt, und jeder weitere Satz scheitert mit "... constant
# tensor cache graph used a different tensor sequence" - bis das Modell
# entladen wird (so in audio.cpp, Stand 2026-09). Der Client entlaedt es
# deshalb selbst und versucht es, wo gewartet werden darf, einmal frisch.
_BROKEN_CACHE_MARKERS = ("different tensor sequence", "tensor sequence mismatch",
                         "saw a new tensor after upload")
_MEMORY_MARKERS = ("failed to allocate", "out of memory", "cudamalloc", "insufficient memory",
                   "not enough memory")
VRAM_HINT = ("auf der Karte von audio.cpp ist zu wenig VRAM frei: dort Platz schaffen (XTTS oder das LLM "
             "auf die andere Karte, bzw. 'Karte von audio.cpp' angeben - dann macht Aktivieren Platz), "
             "ein kleineres Modell oder ein kuerzeres Voice-Sample nehmen (5-15 s; Qwen3 rechnet das "
             "Sample bei jedem Satz mit)")


def _cache_broken(message: str) -> bool:
    text = message.lower()
    return "constant tensor cache" in text and any(marker in text for marker in _BROKEN_CACHE_MARKERS)


def _error_parts(response: httpx.Response) -> tuple[str, str | None]:
    """{"error": {"message", "type"}} von audio.cpp -> (Meldung, Typ)."""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        kind = error.get("type") if isinstance(error, dict) else None
    except ValueError:
        message, kind = None, None
    return str(message or response.text or "ohne Begruendung")[:400], kind


def _describe(message: str, kind: str | None) -> str:
    """Meldung von audio.cpp -> was zu tun ist."""
    unknown = re.match(r"unknown model id: (.+)", message)
    if unknown:
        return (f"Modell '{unknown.group(1)}' ist auf dem audio.cpp-Server nicht (mehr) eingetragen - "
                "dort laden bzw. in die server.json eintragen oder eine andere Engine aktivieren")
    if _cache_broken(message):
        return (f"{message} - das Modell ist in audio.cpp seit einem gescheiterten ersten Satz in einem "
                "kaputten Zustand (Ursache meist zu wenig VRAM). Der Orchestrator hat es entladen; "
                "der naechste Versuch laedt es frisch und zeigt dann die eigentliche Ursache")
    if kind == "insufficient_memory" or any(marker in message.lower() for marker in _MEMORY_MARKERS):
        return f"{message} - {VRAM_HINT}"
    if kind == "server_busy":
        return f"{message} - audio.cpp rechnet gerade etwas anderes mit diesem Modell"
    return message


def _error_message(response: httpx.Response) -> str:
    return _describe(*_error_parts(response))


class AudioCppClient:
    """Ein TTS-Modell auf dem audio.cpp-Server - gemeinsame Engine-
    Schnittstelle wie XTTS & Co.: stream(text, voice_id) liefert
    (Samplerate, PCM16-Chunk)-Tupel.

    Satz fuer Satz ueber POST /v1/audio/speech (WAV zurueck), damit das erste
    Audio nach dem ersten Satz kommt. Geklont wird aus dem Voice-Sample der
    Stimme (Base64, mono PCM16 24 kHz) samt Transkript - ausser der Admin hat
    fuer dieses Modell eine eingebaute Stimme gewaehlt.

    wait_for_load=False (Live-Turn): Ist das Modell nicht geladen, stoesst
    der Client das Laden an und gibt sofort auf (AudioCppNotReady) - dann
    spricht die Rueckfallebene. Probehoeren und Filler warten (True)."""

    def __init__(self, model: str) -> None:
        self.model = model

    async def stream(
        self, text: str, voice_id: str, language: str | None = None, wait_for_load: bool = False
    ) -> AsyncIterator[tuple[int, bytes]]:
        sentences = split_sentences(text)
        url = base_url()
        if not sentences:
            return
        if not url:
            raise AudioCppError(0, "keine audio.cpp-Adresse eingetragen (Sprachausgabe -> audio.cpp)")
        voice = repos.get_voice(voice_id) or {}
        language = language or voice.get("language") or "de"
        builtin = voice_choices().get(self.model)
        family = next((m.get("family", "") for m in cached_models() if m["id"] == self.model), "")
        timeout = httpx.Timeout(10.0, read=600.0 if wait_for_load else 180.0)
        async with httpx.AsyncClient(base_url=url, timeout=timeout) as client:
            if not wait_for_load:
                entry = await self._entry(client)
                family = entry.get("family") or family
                if not entry.get("loaded"):
                    start_warm_up(self.model, voice_id)
                    raise AudioCppNotReady(
                        503, f"Modell '{self.model}' ist nicht geladen - audio.cpp laedt es jetzt")
            for index, sentence in enumerate(sentences):
                payload = self._payload(sentence, voice_id, voice, language, family, builtin, url)
                if index == 0 and not wait_for_load:
                    payload["busy_timeout_ms"] = LIVE_BUSY_TIMEOUT_MS
                pcm, rate = await self._speech(client, payload, url, reload_allowed=wait_for_load)
                for chunk in chunk_pcm(pcm):
                    yield rate, chunk

    async def _entry(self, client: httpx.AsyncClient) -> dict:
        response = await client.get("/v1/models", timeout=STATUS_TIMEOUT_S)
        if response.status_code >= 400:
            raise AudioCppError(response.status_code, _error_message(response))
        for entry in response.json().get("data", []):
            if isinstance(entry, dict) and entry.get("id") == self.model:
                return entry
        raise AudioCppError(404, f"Modell '{self.model}' ist auf dem audio.cpp-Server nicht (mehr) eingetragen")

    def _payload(self, sentence: str, voice_id: str, voice: dict, language: str, family: str,
                 builtin: str | None, url: str) -> dict:
        payload: dict = {"model": self.model, "input": sentence}
        dropped = _dropped.get((url, self.model), set())
        hint = language_hint(family, language)
        if hint and "language" not in dropped:
            payload["language"] = hint
        if builtin:
            payload["voice"] = builtin
            return payload
        reference = _reference(voice_id) if voice_id else None
        if reference:
            payload["voice_ref"] = {"type": "base64", "data": reference}
            transcript = (voice.get("sample_text") or "").strip()
            if transcript and "reference_text" not in dropped:
                payload["reference_text"] = transcript
        return payload

    async def _speech(self, client: httpx.AsyncClient, payload: dict, url: str,
                      reload_allowed: bool) -> tuple[bytes, int]:
        """Ein Satz. reload_allowed: Ist das Modell in audio.cpp kaputt
        (siehe _BROKEN_CACHE_MARKERS), nach dem Entladen einmal frisch
        versuchen - nur wo auf das Neuladen gewartet werden darf."""
        reloaded = False
        for _ in range(4):
            response = await client.post("/v1/audio/speech", json=payload)
            if response.status_code < 400:
                return wav_to_pcm16(response.content)
            message, kind = _error_parts(response)
            rejected = _rejected_fields(message, payload)
            if rejected:
                logger.info("audio.cpp-Modell '%s' lehnt %s ab - frage ohne nach",
                            self.model, ", ".join(sorted(rejected)))
                _dropped.setdefault((url, self.model), set()).update(rejected)
                payload = {key: value for key, value in payload.items() if key not in rejected}
                continue
            if "constant tensor cache" in message.lower():
                # Auch ein gescheitertes Hochladen hinterlaesst den Cache
                # halb gefuellt - so oder so frisch laden lassen.
                await self._reset(message)
                if _cache_broken(message) and reload_allowed and not reloaded:
                    reloaded = True
                    continue
            break
        raise AudioCppError(response.status_code, _describe(message, kind))

    async def _reset(self, reason: str) -> None:
        """Modell auf audio.cpp entladen - der naechste Request laedt es neu."""
        logger.warning("audio.cpp-Modell '%s' in kaputtem Zustand (%s) - wird entladen",
                       self.model, reason[:160])
        try:
            await unload([self.model])
        except Exception as exc:
            logger.warning("audio.cpp-Modell '%s' liess sich nicht entladen: %s", self.model, exc)
