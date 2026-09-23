from collections.abc import AsyncIterator

import httpx

from ..audio import wav_to_pcm16
from ..config import settings


class PiperClient:
    """Client gegen die Filler-Engine (Mikro-Phase 1.9)."""

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        """Text -> (PCM16 mono, Samplerate aus dem WAV-Header)."""
        async with httpx.AsyncClient(base_url=settings.piper_base_url.rstrip("/"), timeout=30.0) as client:
            response = await client.post("/v1/synthesize", json={"text": text})
            response.raise_for_status()
            return wav_to_pcm16(response.content)


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


piper_client = PiperClient()
xtts_client = XttsClient()
