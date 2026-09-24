import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from .. import repos
from ..audio import chunk_pcm, resample_pcm16, wav_to_pcm16
from ..config import settings

# app_settings-Schluessel der optionalen Breeze-Sprechanweisung (Admin-Panel
# "Sprachausgabe"), z. B. "Speak in a warm, calm tone."
BREEZE_INSTRUCTION_SETTING = "breeze_instruction"


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
    """Client gegen den offiziellen Streaming-Server von Breeze TTS 2
    (Test-Engine, v1.17 - Container siehe tts-breeze/).

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
        async with httpx.AsyncClient(base_url=settings.breeze_base_url.rstrip("/"), timeout=300.0) as client:
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
            files = {"ref_audio": (sample.name, sample.read_bytes(), "audio/wav")}
            data["ref_text"] = transcript
        instruction = (repos.get_setting(BREEZE_INSTRUCTION_SETTING) or "").strip()
        if instruction:
            data["instruction"] = instruction
            # Laut Breeze-README folgt das Modell Anweisungen mit CFG 4
            # deutlich besser (Server-Default 1.0).
            data["cfg_scale"] = "4"
        return data, files


piper_client = PiperClient()
xtts_client = XttsClient()
breeze_client = BreezeClient()
