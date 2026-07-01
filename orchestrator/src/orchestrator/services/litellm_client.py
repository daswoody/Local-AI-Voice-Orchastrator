import httpx

from ..config import settings


class LiteLLMClient:
    """Duenner Client gegen LiteLLMs OpenAI-kompatiblen Endpoint (4.6). Tool-Calling
    über das MCP-Gateway folgt erst in Mikro-Phase 1.12 - hier zaehlt nur der
    minimale Text-rein/Text-raus-Pfad aus 1.7."""

    def __init__(self) -> None:
        self._base_url = settings.litellm_base_url.rstrip("/")
        self._api_key = settings.litellm_api_key
        self._model = settings.litellm_model

    async def chat(self, messages: list[dict[str, str]]) -> str:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=60.0) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "messages": messages},
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]


litellm_client = LiteLLMClient()
