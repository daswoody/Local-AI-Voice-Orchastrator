import json
import logging
from collections.abc import Awaitable, Callable

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

# Denk-Router (v1.27): schaltet das Nachdenken fuer einen Call ab. LiteLLM
# verwirft ein direktes reasoning_effort fuer LM-Studio-Modelle
# stillschweigend - in extra_body reicht es den Wert dagegen unveraendert
# durch (am echten Setup gemessen: 0 statt ~300 Denk-Token, 0,6 s statt
# ~11 s). chat_template_kwargs/enable_thinking ignoriert LM Studio.
_NO_THINKING = {"extra_body": {"reasoning_effort": "none"}}


def _rejects_reasoning(payload: dict, status_code: int, body: str) -> bool:
    """Modell ohne Reasoning-Einstellung lehnt den Parameter ab: dann eben
    mit Modell-Default fragen - Denken war ohnehin nicht da."""
    if status_code >= 400 and "extra_body" in payload and "reasoning" in body.lower():
        logger.warning("Modell %s kennt reasoning_effort nicht - frage ohne", payload["model"])
        payload.pop("extra_body")
        return True
    return False


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
                           model: str | None = None, think: bool | None = None) -> dict:
        """Liefert die komplette Assistant-Message (content UND tool_calls) -
        der Agent-Loop (1.12) braucht beides. `model` ueberschreibt das
        aktive Modell fuer diesen Call (Agenten 4.16 haben eigene Modelle).
        `think` (Denk-Router, v1.27): False schaltet das Nachdenken ab,
        True/None laesst den Modell-Default (Gemma 4 in LM Studio: denkt)."""
        payload: dict = {"model": model or self.active_model(), "messages": messages}
        if tools:
            payload["tools"] = tools
        if think is False:
            payload.update(_NO_THINKING)

        async with httpx.AsyncClient(base_url=self._base_url, timeout=120.0) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            if _rejects_reasoning(payload, response.status_code, response.text):
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

    async def stream_chat_message(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        model: str | None = None,
        think: bool | None = None,
    ) -> dict:
        """Wie chat_message, aber mit stream=true (v1.26): jedes Text-Delta
        geht sofort an on_delta, damit Chat und Sprachausgabe nicht auf das
        Ende der Antwort warten. Tool-Calls kommen in Teilstuecken (Name,
        Argumente als JSON-Fragmente) und werden hier wieder zur
        kompletten Assistant-Message zusammengesetzt. `think` wie bei
        chat_message."""
        payload: dict = {"model": model or self.active_model(), "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        if think is False:
            payload.update(_NO_THINKING)

        content: list[str] = []
        tool_calls: dict[int, dict] = {}
        async with httpx.AsyncClient(base_url=self._base_url, timeout=120.0) as client:
            for attempt in range(2):
                async with client.stream(
                    "POST", "/v1/chat/completions", headers=self._headers(), json=payload
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode(errors="replace")
                        if attempt == 0 and _rejects_reasoning(payload, response.status_code, body):
                            continue
                        raise RuntimeError(
                            f"LLM-Fehler {response.status_code} (Modell {payload['model']}): {body[:400]}"
                        )
                    await self._read_stream(response, payload, content, tool_calls, on_delta)
                    break

        message: dict = {"role": "assistant", "content": "".join(content) or None}
        if tool_calls:
            message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
        return message

    @staticmethod
    async def _read_stream(
        response: httpx.Response,
        payload: dict,
        content: list[str],
        tool_calls: dict[int, dict],
        on_delta: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        """SSE-Zeilen lesen: Text-Deltas sammeln und weiterreichen,
        Tool-Call-Teilstuecke zusammensetzen."""
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise RuntimeError(f"LLM-Fehler im Stream (Modell {payload['model']}): "
                                   f"{str(chunk['error'])[:400]}")
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            text = delta.get("content")
            if text:
                content.append(text)
                if on_delta is not None:
                    await on_delta(text)
            for part in delta.get("tool_calls") or []:
                _merge_tool_call(tool_calls, part)

    async def chat(self, messages: list[dict], think: bool | None = None) -> str:
        """Bequemer Text-only-Pfad (ohne Tools)."""
        message = await self.chat_message(messages, think=think)
        return message.get("content") or ""


def _merge_tool_call(tool_calls: dict[int, dict], part: dict) -> None:
    """Ein Tool-Call-Delta (OpenAI-Streaming-Format) in den Sammler
    einarbeiten: id und Name kommen einmal, die Argumente stueckweise."""
    index = part.get("index", 0)
    entry = tool_calls.setdefault(
        index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
    )
    if part.get("id"):
        entry["id"] = part["id"]
    function = part.get("function") or {}
    if function.get("name") and not entry["function"]["name"]:
        entry["function"]["name"] = function["name"]
    if function.get("arguments"):
        entry["function"]["arguments"] += function["arguments"]


litellm_client = LiteLLMClient()
