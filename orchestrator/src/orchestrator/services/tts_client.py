import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .. import repos
from ..audio import chunk_pcm, pcm16_to_wav, resample_pcm16, wav_to_pcm16
from ..config import settings

logger = logging.getLogger(__name__)

# Breeze rechnet intern mit 24 kHz; die Referenz geht schon passend hin.
BREEZE_SAMPLE_RATE = 24000

# app_settings-Schluessel der optionalen Breeze-Sprechanweisung (Admin-Panel
# "Sprachausgabe"), z. B. "Speak in a warm, calm tone."
BREEZE_INSTRUCTION_SETTING = "breeze_instruction"
# ... und der Breeze-Server-Adresse (v1.18). Leer = BREEZE_BASE_URL aus der .env.
BREEZE_URL_SETTING = "breeze_base_url"


def breeze_base_url() -> str:
    """Adresse des Breeze-Servers: im Panel gesetzt, sonst .env. Beide
    Varianten - der offizielle PyTorch-Server und Breeze-TTS-2.cpp - sprechen
    dieselbe Schnittstelle, der Orchestrator muss nicht wissen, welche laeuft."""
    return (repos.get_setting(BREEZE_URL_SETTING) or settings.breeze_base_url).rstrip("/")


def normalize_base_url(value: str) -> str:
    """Eingabe aus dem Panel -> Basis-URL ('' = Standard aus der .env).
    Ohne Schema wird http:// ergaenzt ("192.168.2.105:7860",
    "breeze.example.org"); erlaubt sind http/https mit Host, optional Port und
    Pfad-Praefix (Reverse-Proxy)."""
    value = value.strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Adresse braucht die Form http(s)://host[:port][/pfad]")
    if parts.query or parts.fragment:
        raise ValueError("Adresse ohne ?-Parameter oder #-Anker angeben")
    parts.port  # wirft ValueError bei ungueltigem Port
    return value.rstrip("/")


class PiperClient:
    """Client gegen die Filler-Engine (Mikro-Phase 1.9)."""

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        """Text -> (PCM16 mono, Samplerate aus dem WAV-Header)."""
        async with httpx.AsyncClient(base_url=settings.piper_base_url.rstrip("/"), timeout=30.0) as client:
            response = await client.post("/v1/synthesize", json={"text": text})
            response.raise_for_status()
            return wav_to_pcm16(response.content)

    async def stream(
        self, text: str, voice_id: str | None = None, language: str | None = None
    ) -> AsyncIterator[tuple[int, bytes]]:
        """Gemeinsame Engine-Schnittstelle (v1.17), damit Piper auch die
        Hauptantwort sprechen kann. Piper liefert das WAV am Stueck - hier
        auf die Stream-Rate gebracht und in 1-s-Chunks geteilt. voice_id
        spielt keine Rolle: Piper hat genau eine Stimme."""
        pcm, rate = await self.synthesize(text)
        pcm = resample_pcm16(pcm, rate, settings.target_sample_rate)
        for chunk in chunk_pcm(pcm):
            yield settings.target_sample_rate, chunk


class XttsClient:
    """Client gegen die Hauptstimmen-Engine (Mikro-Phase 1.10)."""

    async def stream(
        self, text: str, voice_id: str, language: str | None = None
    ) -> AsyncIterator[tuple[int, bytes]]:
        """Text -> async Iterator von (Samplerate, PCM16-Chunk).

        Die Rate kommt pro Tupel mit, weil sie erst mit den Response-Headern
        bekannt ist - der Aufrufer soll nicht auf ein separates Attribut
        warten muessen, das erst nach dem ersten await gefuellt waere."""
        payload = {"text": text, "voice_id": voice_id}
        if language:
            payload["language"] = language

        async with httpx.AsyncClient(base_url=settings.xtts_base_url.rstrip("/"), timeout=300.0) as client:
            async with client.stream("POST", "/v1/synthesize", json=payload) as response:
                if response.status_code >= 400:
                    # Fehler-Body explizit lesen: bei Streams ist er sonst
                    # nicht verfuegbar (raise_for_status + .text wuerde an
                    # ResponseNotRead scheitern) - und der XTTS-Service
                    # liefert seit dem Stream-Priming echte Fehlertexte.
                    detail = (await response.aread()).decode("utf-8", "replace")
                    raise RuntimeError(f"XTTS-Fehler {response.status_code}: {detail[:300]}")
                rate = int(response.headers.get("x-sample-rate", "24000"))
                async for chunk in response.aiter_bytes(chunk_size=48000):
                    if chunk:
                        yield rate, chunk

    async def synthesize(
        self, text: str, voice_id: str, language: str | None = None,
        temperature: float | None = None,
    ) -> tuple[bytes, int]:
        """Text -> komplettes (PCM16 mono, Samplerate), nicht streamend (v1.16).

        Fuer die Filler-Vorgenerierung: Latenz zaehlt dort nicht, also nutzt
        XTTS seinen Qualitaetspfad statt des Streamings (keine Chunk-Naehte).
        `temperature` = vorsichtigeres Sampling fuer einen Neuversuch."""
        payload: dict = {"text": text, "voice_id": voice_id}
        if language:
            payload["language"] = language
        if temperature is not None:
            payload["temperature"] = temperature

        async with httpx.AsyncClient(base_url=settings.xtts_base_url.rstrip("/"), timeout=300.0) as client:
            response = await client.post("/v1/synthesize/full", json=payload)
            if response.status_code >= 400:
                raise RuntimeError(f"XTTS-Fehler {response.status_code}: {response.text[:300]}")
            return wav_to_pcm16(response.content)


class BreezeClient:
    """Client gegen einen Breeze-TTS-2-Server (Test-Engine, v1.17): den
    offiziellen PyTorch-Server (tts-breeze/) oder Breeze-TTS-2.cpp
    (tts-breeze-cpp/ bzw. nativ, v1.18) - gleiche HTTP-Schnittstelle.

    Anders als XTTS kennt Breeze keine voice_id: Fuers Voice-Cloning gehen
    das Sample UND sein exaktes Transkript in jedem Request mit (Multipart).
    Fehlt eins von beiden, spricht Breeze mit seiner eingebauten Stimme.
    Die Antwort ist wie bei XTTS roher PCM16-Stream mit X-Sample-Rate."""

    # Der Server bearbeitet genau EINEN Request zur Zeit und lehnt weitere
    # mit 409 ab (z. B. Filler-Generierung waehrend eines Turns) - kurz
    # warten statt sofort aufgeben.
    busy_retries = 10
    busy_wait_s = 0.5

    async def stream(
        self, text: str, voice_id: str, language: str | None = None
    ) -> AsyncIterator[tuple[int, bytes]]:
        """Text -> async Iterator von (Samplerate, PCM16-Chunk). language
        wird ignoriert: Breeze erkennt die Sprache am Text."""
        data, files = self._form(text, voice_id)
        async with httpx.AsyncClient(base_url=breeze_base_url(), timeout=300.0) as client:
            for attempt in range(self.busy_retries + 1):
                async with client.stream("POST", "/v1/audio/speech", data=data, files=files) as response:
                    if response.status_code == 409 and attempt < self.busy_retries:
                        await asyncio.sleep(self.busy_wait_s)
                        continue
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode("utf-8", "replace")
                        raise RuntimeError(f"Breeze-Fehler {response.status_code}: {detail[:300]}")
                    rate = int(response.headers.get("x-sample-rate", "24000"))
                    async for chunk in response.aiter_bytes(chunk_size=48000):
                        if chunk:
                            yield rate, chunk
                    return

    @staticmethod
    def _form(text: str, voice_id: str) -> tuple[dict, dict | None]:
        data = {"text": text}
        files = None
        sample = Path(settings.voices_dir) / f"{voice_id}.wav"
        voice = repos.get_voice(voice_id) or {}
        transcript = (voice.get("sample_text") or "").strip()
        if sample.exists() and transcript:
            files = {"ref_audio": (sample.name, _reference_wav(sample.read_bytes()), "audio/wav")}
            data["ref_text"] = transcript
        instruction = (repos.get_setting(BREEZE_INSTRUCTION_SETTING) or "").strip()
        if instruction:
            data["instruction"] = instruction
            # Laut Breeze-README folgt das Modell Anweisungen mit CFG 4
            # deutlich besser (Server-Default 1.0).
            data["cfg_scale"] = "4"
        return data, files


def _reference_wav(raw: bytes) -> bytes:
    """Voice-Sample -> mono PCM16 mit 24 kHz fuer Breeze.

    Der Upload nimmt jedes PCM-WAV an (XTTS liest alles), der WAV-Leser von
    Breeze-TTS-2.cpp kennt aber nur 16/32 Bit: Ein 24-Bit-Sample kaeme dort
    als reine Stille an, geklont wuerde dann ein stummes Sample. Deshalb
    hier einheitlich umwandeln - das spart dem Server nebenbei das
    Resampling. Kann der Orchestrator das Sample selbst nicht lesen, geht
    es unveraendert raus."""
    try:
        pcm, rate = wav_to_pcm16(raw)
    except Exception as exc:
        logger.warning("Voice-Sample fuer Breeze nicht umwandelbar, sende Original: %s", exc)
        return raw
    return pcm16_to_wav(resample_pcm16(pcm, rate, BREEZE_SAMPLE_RATE), BREEZE_SAMPLE_RATE)


piper_client = PiperClient()
xtts_client = XttsClient()
breeze_client = BreezeClient()
