"""Client fuer LM Studios native REST-API (ab Version 0.4.0) - Grundlage des
Modell-Panels im Admin-Frontend (4.14: Hot-Swap als bewusste Admin-Aktion).

Endpoints laut LM-Studio-Doku (https://lmstudio.ai/docs/developer/rest):
GET /api/v1/models (inkl. Ladezustand), POST /api/v1/models/load,
POST /api/v1/models/unload. Beim ersten Einsatz auf der VM verifizieren -
die API ist relativ neu."""

import httpx

from ..config import settings


class LMStudioClient:
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=settings.lmstudio_base_url.rstrip("/"), timeout=120.0)

    async def list_models(self) -> list[dict]:
        async with self._client() as client:
            response = await client.get("/api/v1/models")
            response.raise_for_status()
            data = response.json()
            # Antwortform variiert leicht zwischen Versionen ({"data": [...]}
            # vs. {"models": [...]}) - beide abdecken.
            return data.get("data") or data.get("models") or []

    async def load_model(self, model_key: str) -> dict:
        async with self._client() as client:
            response = await client.post("/api/v1/models/load", json={"model_key": model_key})
            response.raise_for_status()
            return response.json()

    async def unload_model(self, instance_id: str) -> dict:
        async with self._client() as client:
            response = await client.post("/api/v1/models/unload", json={"instance_id": instance_id})
            response.raise_for_status()
            return response.json()


lmstudio_client = LMStudioClient()
