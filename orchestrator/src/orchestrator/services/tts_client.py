import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .. import repos
from ..audio import chunk_pcm, pcm16_to_wav, resample_pcm16, wav_to_pcm16
from ..config import settings

logger = logging.getLogger(__name__)

# Klonende Engines bekommen das Voice-Sample einheitlich als mono PCM16 mit
# 24 kHz (siehe reference_wav).
REFERENCE_SAMPLE_RATE = 24000


def normalize_base_url(value: str) -> str:
    """Eingabe aus dem Panel -> Basis-URL ('' = Standard aus der .env).
    Ohne Schema wird http:// ergaenzt ("192.168.2.105:8080",
    "audiocpp.example.org"); erlaubt sind http/https mit Host, optional Port
    und Pfad-Praefix (Reverse-Proxy)."""
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


class ContractEngineError(RuntimeError):
    """Antwort einer Vertrags-Engine mit HTTP-Fehler (Text = ihr detail)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{detail} (HTTP {status_code})")
        self.status_code = status_code


class ContractClient:
    """Client fuer jede Engine nach dem Engine-Vertrag v1 (tts-engine-kit,
    v1.20) - ein Client fuer alle, die Engine sagt per /v1/info selbst, was
    sie kann.

    Pro Request gehen Text, Sprache der Stimme und - falls vorhanden - das
    Voice-Sample (als mono PCM16 24 kHz) samt Transkript mit; die Engine
    entscheidet, was sie davon nutzt, und cacht die Referenz selbst.
    load_timeout_s: 0 = nicht aufs Laden warten (Live-Turn: dann springt die
    Rueckfallebene ein), beim Probehoeren/Filler ein paar Minuten."""

    # Die Engine wartet selbst auf eine laufende Synthese; 409 kommt erst,
    # wenn das zu lange dauert - dann nur noch kurz nachfassen.
    busy_retries = 2
    busy_wait_s = 1.0

    def __init__(self, base_url) -> None:
        self._base_url = base_url

    async def stream(
        self, text: str, voice_id: str, language: str | None = None, load_timeout_s: float = 0.0
    ) -> AsyncIterator[tuple[int, bytes]]:
        data, files = self._form(text, voice_id, language, load_timeout_s)
        timeout = httpx.Timeout(30.0, read=max(300.0, load_timeout_s + 60.0))
        async with httpx.AsyncClient(base_url=self._base_url(), timeout=timeout) as client:
            for attempt in range(self.busy_retries + 1):
                async with client.stream("POST", "/v1/synthesize", data=data, files=files) as response:
                    if response.status_code == 409 and attempt < self.busy_retries:
                        await asyncio.sleep(self.busy_wait_s)
                        continue
                    if response.status_code >= 400:
                        raise ContractEngineError(response.status_code, _detail(await response.aread()))
                    rate = int(response.headers.get("x-sample-rate", "24000"))
                    async for chunk in response.aiter_bytes(chunk_size=48000):
                        if chunk:
                            yield rate, chunk
                    return

    @staticmethod
    def _form(text: str, voice_id: str, language: str | None, load_timeout_s: float) -> tuple[dict, dict | None]:
        voice = repos.get_voice(voice_id) or {}
        data = {
            "text": text,
            "voice_id": voice_id,
            "language": language or voice.get("language") or "de",
            "load_timeout_s": str(load_timeout_s),
        }
        files = None
        sample = Path(settings.voices_dir) / f"{voice_id}.wav"
        if sample.exists():
            files = {"ref_audio": (sample.name, reference_wav(sample.read_bytes()), "audio/wav")}
            transcript = (voice.get("sample_text") or "").strip()
            if transcript:
                data["ref_text"] = transcript
        return data, files


def _detail(body: bytes) -> str:
    """{"detail": "..."} der Engine lesbar machen."""
    text = body.decode("utf-8", "replace")
    try:
        detail = json.loads(text).get("detail")
    except (ValueError, AttributeError):
        detail = None
    return str(detail or text or "ohne Begruendung")[:400]


def reference_wav(raw: bytes) -> bytes:
    """Voice-Sample -> mono PCM16 mit 24 kHz fuer audio.cpp und
    Vertrags-Engines.

    Der Upload nimmt jedes PCM-WAV an (XTTS liest alles), viele
    WAV-Leser in C++-Engines kennen aber nur 16/32 Bit - ein 24-Bit-Sample
    kaeme dort als reine Stille an (so bei Breeze-TTS-2.cpp beobachtet,
    v1.18.2). Deshalb hier einheitlich umwandeln; das spart dem Server
    nebenbei das Resampling. Kann der Orchestrator das Sample selbst nicht
    lesen, geht es unveraendert raus."""
    try:
        pcm, rate = wav_to_pcm16(raw)
    except Exception as exc:
        logger.warning("Voice-Sample nicht umwandelbar, sende Original: %s", exc)
        return raw
    return pcm16_to_wav(resample_pcm16(pcm, rate, REFERENCE_SAMPLE_RATE), REFERENCE_SAMPLE_RATE)


piper_client = PiperClient()
xtts_client = XttsClient()
