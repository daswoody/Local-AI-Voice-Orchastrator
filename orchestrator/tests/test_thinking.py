"""Denk-Router (v1.27): pro Frage entscheiden, ob das LLM nachdenkt."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from orchestrator import graph as graph_module
from orchestrator import repos
from orchestrator.audio import pcm_to_b64
from orchestrator.config import settings
from orchestrator.routers import stream as stream_module
from orchestrator.services import litellm_client as litellm_module
from orchestrator.services import thinking_router
from orchestrator.services.tts_client import xtts_client


@pytest.mark.parametrize(
    "text",
    [
        "Wie spät ist es?",
        "Mach das Licht im Wohnzimmer an",
        "Wie wird das Wetter morgen?",
        "Erzähl einen Witz",
        # Einfaches Kopfrechnen schafft das Modell ohne Denken in ~0,5 s.
        "Was ist 17*23?",
        # Zahlen ohne Rechenzeichen sind keine Rechenaufgabe.
        "Erinnere mich am 14.10.2026 um 14:30",
        "Es sind minus 5 Grad draußen",
    ],
)
def test_everyday_requests_answer_without_thinking(text):
    assert thinking_router.classify(text).think is False


@pytest.mark.parametrize(
    "text",
    [
        "Warum ist der Himmel blau?",
        "Erklär mir, wie eine Wärmepumpe funktioniert",
        "Vergleiche Python und Rust",
        "Plane meinen Tag",
        "Soll ich heute joggen oder ins Kino?",
        "Was ist 17 mal 23 plus 5?",
        "Was ist 1234 mal 56?",
        "Was sind 15 Prozent von 80?",
        "Denk mal genau nach: wie viele Beine haben drei Hühner?",
        " ".join(["wort"] * thinking_router.LONG_REQUEST_WORDS),
    ],
)
def test_hard_requests_get_thinking(text):
    assert thinking_router.classify(text).think is True


def test_image_input_gets_thinking():
    decision = thinking_router.classify("Was siehst du?", has_image=True)
    assert decision.think is True and decision.reason == "Bild-Eingabe"


def test_admin_mode_overrides_the_classifier():
    assert thinking_router.current_mode() == "auto"
    repos.set_setting(thinking_router.MODE_SETTING, "on")
    assert thinking_router.decide("Hallo").think is True
    repos.set_setting(thinking_router.MODE_SETTING, "off")
    assert thinking_router.decide("Warum ist der Himmel blau?").think is False
    # Unbekannter Wert in der DB faellt auf auto zurueck statt zu crashen.
    repos.set_setting(thinking_router.MODE_SETTING, "kaputt")
    assert thinking_router.current_mode() == "auto"


def test_thinking_mode_admin_endpoints(client, admin_headers):
    assert client.get("/v1/admin/thinking", headers=admin_headers).json() == {"mode": "auto"}
    response = client.post("/v1/admin/thinking", json={"mode": "off"}, headers=admin_headers)
    assert response.json() == {"mode": "off"}
    assert thinking_router.current_mode() == "off"
    bad = client.post("/v1/admin/thinking", json={"mode": "manchmal"}, headers=admin_headers)
    assert bad.status_code == 422


# ---- LiteLLM-Payload -------------------------------------------------------------------


def _capture_litellm(monkeypatch, responder=None) -> list[dict]:
    """Faengt die an LiteLLM geschickten Payloads ab (kein Netzwerk)."""
    payloads: list[dict] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if responder is not None:
            return responder(payload)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "391"}}]}
        )

    monkeypatch.setattr(
        litellm_module.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return payloads


async def test_think_false_sends_reasoning_effort_none_in_extra_body(monkeypatch):
    """Direktes reasoning_effort verwirft LiteLLM fuer LM-Studio-Modelle -
    nur ueber extra_body kommt es bei LM Studio an (am Setup gemessen)."""
    payloads = _capture_litellm(monkeypatch)
    client = litellm_module.LiteLLMClient()

    await client.chat_message([{"role": "user", "content": "17*23?"}], think=False)
    await client.chat_message([{"role": "user", "content": "17*23?"}], think=True)
    await client.chat_message([{"role": "user", "content": "17*23?"}])

    assert payloads[0]["extra_body"] == {"reasoning_effort": "none"}
    assert "reasoning_effort" not in payloads[0]
    # Denken an = Modell-Default, nichts mitschicken.
    assert "extra_body" not in payloads[1]
    assert "extra_body" not in payloads[2]


async def test_model_without_reasoning_support_is_asked_again_without(monkeypatch):
    def responder(payload):
        if "extra_body" in payload:
            return httpx.Response(400, text="Model does not support reasoning_effort")
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    payloads = _capture_litellm(monkeypatch, responder)
    message = await litellm_module.LiteLLMClient().chat_message(
        [{"role": "user", "content": "Hallo"}], think=False
    )

    assert message["content"] == "ok"
    assert len(payloads) == 2 and "extra_body" not in payloads[1]


def _sse_answer(text: str) -> httpx.Response:
    body = (f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n"
            "data: [DONE]\n\n")
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


async def test_streaming_call_switches_thinking_off_the_same_way(monkeypatch):
    payloads = _capture_litellm(monkeypatch, lambda payload: _sse_answer("391"))
    client = litellm_module.LiteLLMClient()

    message = await client.stream_chat_message([{"role": "user", "content": "17*23?"}], think=False)
    await client.stream_chat_message([{"role": "user", "content": "17*23?"}], think=True)

    assert message["content"] == "391"
    assert payloads[0]["stream"] is True
    assert payloads[0]["extra_body"] == {"reasoning_effort": "none"}
    assert "extra_body" not in payloads[1]


async def test_streaming_retries_without_reasoning_for_models_without_support(monkeypatch):
    def responder(payload):
        if "extra_body" in payload:
            return httpx.Response(400, text="reasoning_effort not supported")
        return _sse_answer("ok")

    payloads = _capture_litellm(monkeypatch, responder)
    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    message = await litellm_module.LiteLLMClient().stream_chat_message(
        [{"role": "user", "content": "Hallo"}], on_delta=on_delta, think=False
    )

    assert message["content"] == "ok" and deltas == ["ok"]
    assert len(payloads) == 2 and "extra_body" not in payloads[1]


async def test_other_errors_are_not_retried(monkeypatch):
    payloads = _capture_litellm(monkeypatch, lambda payload: httpx.Response(500, text="kaputt"))
    with pytest.raises(RuntimeError, match="LLM-Fehler 500"):
        await litellm_module.LiteLLMClient().chat_message(
            [{"role": "user", "content": "Hallo"}], think=False
        )
    assert len(payloads) == 1


# ---- Pipeline ------------------------------------------------------------------------


def _patch_llm(monkeypatch, delay: float = 0.0) -> list[bool | None]:
    calls: list[bool | None] = []

    async def fake_chat(messages, tools=None, think=None):
        calls.append(think)
        if delay:
            await asyncio.sleep(delay)
        return {"role": "assistant", "content": "Antwort"}

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", fake_chat)
    return calls


def _text_turn(client, text: str) -> list[dict]:
    with client.websocket_connect("/v1/assistant/stream") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "hello", "mode": "chat"})
        ws.send_json({"type": "text_input", "text": text})
        frames = []
        while (frame := ws.receive_json())["type"] != "done":
            frames.append(frame)
    return frames


def test_router_decision_reaches_the_llm_call(client, monkeypatch):
    calls = _patch_llm(monkeypatch)

    _text_turn(client, "Hallo")
    _text_turn(client, "Warum ist der Himmel blau?")
    repos.set_setting(thinking_router.MODE_SETTING, "off")
    _text_turn(client, "Warum ist der Himmel blau?")

    assert calls == [False, True, False]


def test_router_decision_reaches_the_streaming_llm_call(client, monkeypatch):
    calls: list[bool | None] = []

    async def fake_stream(messages, tools=None, on_delta=None, model=None, think=None):
        calls.append(think)
        if on_delta is not None:
            await on_delta("Antwort.")
        return {"role": "assistant", "content": "Antwort."}

    monkeypatch.setattr(settings, "llm_streaming_enabled", True)
    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "stream_chat_message", fake_stream)

    _text_turn(client, "Mach das Licht an")
    _text_turn(client, "Vergleiche Python und Rust")

    assert calls == [False, True]


def _talk_turn(client, monkeypatch, transcript: str) -> list[str]:
    monkeypatch.setattr(stream_module.stt_client, "transcribe", AsyncMock(return_value=transcript))
    monkeypatch.setattr(
        stream_module.piper_client, "synthesize",
        AsyncMock(return_value=(b"\x05\x06" * 2205, 22050)),
    )

    async def fake_xtts_stream(text, voice_id, language=None):
        yield 24000, b"\x01\x02" * 1200

    monkeypatch.setattr(xtts_client, "stream", fake_xtts_stream)
    # Filler-Delay weit ueber der LLM-Dauer: ohne Denk-Router kaeme er nie.
    for filler in repos.fillers_for_kind("thinking"):
        repos.update_filler(filler["id"], filler["title"], filler["text"],
                            filler["trigger_id"], True, delay_ms=5000)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "hello", "mode": "talk"})
        ws.send_json({"type": "audio_chunk", "data": pcm_to_b64(b"\x00\x00" * 1600)})
        ws.send_json({"type": "audio_end"})
        types = []
        while (frame := ws.receive_json())["type"] != "done":
            types.append(frame["type"])
    return types


def test_thinking_turn_plays_filler_immediately(client, monkeypatch):
    _patch_llm(monkeypatch, delay=0.3)
    types = _talk_turn(client, monkeypatch, "Warum regnet es heute?")
    assert types.index("audio_chunk") < types.index("assistant_text")


def test_fast_turn_keeps_the_filler_delay(client, monkeypatch):
    _patch_llm(monkeypatch, delay=0.3)
    types = _talk_turn(client, monkeypatch, "Wie ist das Wetter?")
    assert "audio_chunk" not in types[: types.index("assistant_text")]


async def test_voice_summary_never_thinks(monkeypatch):
    calls: list[bool | None] = []

    async def fake_chat_message(messages, tools=None, model=None, think=None):
        calls.append(think)
        return {"role": "assistant", "content": "Kurzfassung."}

    monkeypatch.setattr(litellm_module.litellm_client, "chat_message", fake_chat_message)
    # _speech_text braucht keinen Verbindungszustand - ohne WebSocket bauen.
    session = stream_module.StreamSession.__new__(stream_module.StreamSession)

    assert await session._speech_text("Sehr lange Antwort. " * 50) == "Kurzfassung."
    assert calls == [False]
