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
                response.raise_for_status()
                rate = int(response.headers.get("x-sample-rate", "24000"))
                async for chunk in response.aiter_bytes(chunk_size=48000):
                    if chunk:
                        yield rate, chunk


piper_client = PiperClient()
xtts_client = XttsClient()
