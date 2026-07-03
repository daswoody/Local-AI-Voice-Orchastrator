import httpx

from ..config import settings


class SttClient:
    """Client gegen den STT-Service (Mikro-Phase 1.8)."""

    async def transcribe(self, pcm16: bytes, sample_rate: int) -> str:
        async with httpx.AsyncClient(base_url=settings.stt_base_url.rstrip("/"), timeout=120.0) as client:
            response = await client.post(
                "/v1/transcribe",
                params={"sample_rate": sample_rate},
                content=pcm16,
                headers={"content-type": "application/octet-stream"},
            )
            response.raise_for_status()
            return response.json()["text"]


stt_client = SttClient()
