import httpx

from ..config import settings


class LiteLLMClient:
    """Duenner Client gegen LiteLLMs OpenAI-kompatiblen Endpoint (4.6)."""

    def __init__(self) -> None:
        self._base_url = settings.litellm_base_url.rstrip("/")
        self._api_key = settings.litellm_api_key
        self._model = settings.litellm_model

    async def chat_message(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Liefert die komplette Assistant-Message (content UND tool_calls) -
        der Agent-Loop (1.12) braucht beides."""
        payload: dict = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(base_url=self._base_url, timeout=120.0) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]

    async def chat(self, messages: list[dict]) -> str:
        """Bequemer Text-only-Pfad (ohne Tools)."""
        message = await self.chat_message(messages)
        return message.get("content") or ""


litellm_client = LiteLLMClient()
