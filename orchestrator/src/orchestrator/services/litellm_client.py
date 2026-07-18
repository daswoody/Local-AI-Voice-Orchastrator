import httpx

from ..config import settings


class LiteLLMClient:
    """Client gegen LiteLLM - den EINZIGEN LLM-Zugang des Projekts (4.6).
    LM Studio haengt dahinter als Provider; der Orchestrator spricht nie
    direkt mit LM Studio."""

    def __init__(self) -> None:
        self._base_url = settings.litellm_base_url.rstrip("/")
        self._api_key = settings.litellm_api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def active_model(self) -> str:
        """Das im Admin-Panel gewaehlte Modell (app_settings), Fallback auf
        die .env. Pro Call aufgeloest, damit ein Wechsel sofort greift."""
        try:
            from .. import repos

            return repos.get_setting("active_model") or settings.litellm_model
        except Exception:
            # DB (noch) nicht initialisiert -> .env-Default statt Crash.
            return settings.litellm_model

    async def list_models(self) -> list[dict]:
        """Modelle des LiteLLM-Proxys (= dort registrierte LM-Studio-Modelle)."""
        async with httpx.AsyncClient(base_url=self._base_url, timeout=30.0) as client:
            response = await client.get("/v1/models", headers=self._headers())
            response.raise_for_status()
            return response.json().get("data", [])

    async def chat_message(self, messages: list[dict], tools: list[dict] | None = None,
                           model: str | None = None) -> dict:
        """Liefert die komplette Assistant-Message (content UND tool_calls) -
        der Agent-Loop (1.12) braucht beides. `model` ueberschreibt das
        aktive Modell fuer diesen Call (Agenten 4.16 haben eigene Modelle)."""
        payload: dict = {"model": model or self.active_model(), "messages": messages}
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(base_url=self._base_url, timeout=120.0) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            if response.status_code >= 400:
                # Fehlerbody von LiteLLM/LM Studio in die Exception heben
                # (z. B. Template-/Tool-Fehler lokaler Modelle) - sonst
                # steht im Log nur ein nichtssagender Statuscode.
                raise RuntimeError(
                    f"LLM-Fehler {response.status_code} (Modell {payload['model']}): "
                    f"{response.text[:400]}"
                )
            return response.json()["choices"][0]["message"]

    async def chat(self, messages: list[dict]) -> str:
        """Bequemer Text-only-Pfad (ohne Tools)."""
        message = await self.chat_message(messages)
        return message.get("content") or ""


litellm_client = LiteLLMClient()
