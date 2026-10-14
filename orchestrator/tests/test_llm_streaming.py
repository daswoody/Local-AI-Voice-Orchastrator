"""LLM-Streaming (v1.26): Antwort Token fuer Token holen, im Chat
mitschreiben und Satz fuer Satz sprechen, waehrend das LLM noch schreibt."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from orchestrator import graph as graph_module
from orchestrator import repos
from orchestrator.audio import pcm_to_b64
from orchestrator.config import settings
from orchestrator.routers import stream as stream_module
from orchestrator.services import litellm_client as litellm_module
from orchestrator.services import tts_engines
from orchestrator.services.sentences import SentenceStream, clean_for_speech
from orchestrator.services.tts_client import piper_client, xtts_client


def _stream(text: str, step: int = 4) -> list[str]:
    sentences = SentenceStream()
    pieces = []
    for offset in range(0, len(text), step):
        pieces += sentences.feed(text[offset:offset + step])
    return pieces + sentences.flush()


# ---- Satz-Puffer ------------------------------------------------------------------


def test_sentence_stream_cuts_at_sentence_ends_not_at_ordinals_or_abbreviations():
    text = ("Das Treffen ist am 3. Oktober mit Dr. Meier. Es kostet 3.5 Euro, also z. B. "
            "weniger als gedacht. Ja. Danach geht es weiter!")
    assert _stream(text) == [
        "Das Treffen ist am 3. Oktober mit Dr. Meier.",
        "Es kostet 3.5 Euro, also z. B. weniger als gedacht.",
        # Zu kurze Saetze wandern zum naechsten.
        "Ja. Danach geht es weiter!",
    ]


def test_sentence_stream_cuts_the_first_piece_early_at_a_comma():
    """Der erste Ton soll frueh kommen: das erste Stueck endet schon am
    ersten Komma nach ein paar Woertern, die weiteren an Satzenden."""
    text = "Morgen wird es in Berlin sonnig und warm, am Abend zieht leichter Regen auf, sagt der Dienst."
    assert _stream(text) == [
        "Morgen wird es in Berlin sonnig und warm,",
        "am Abend zieht leichter Regen auf, sagt der Dienst.",
    ]


def test_sentence_stream_waits_for_whitespace_after_the_period():
    sentences = SentenceStream()
    # "3." koennte "3.5" werden - noch kein Schnitt.
    assert sentences.feed("Der Preis liegt heute bei 3.") == []
    assert sentences.feed("5 Euro pro Stueck. Und") == ["Der Preis liegt heute bei 3.5 Euro pro Stueck."]
    assert sentences.flush() == ["Und"]


def test_sentence_stream_drops_markdown_emojis_and_think_blocks():
    text = "<think>Erst nachdenken. Lange.</think>**Klar**, hier die Liste:\n- Milch 🥛\n- Brot\nFertig"
    assert _stream(text) == ["Klar, hier die Liste:", "Milch", "Brot", "Fertig"]
    assert clean_for_speech("Siehe [Wetterdienst](https://example.org) 😀") == "Siehe Wetterdienst"


def test_sentence_stream_drops_an_unfinished_think_block():
    sentences = SentenceStream()
    assert sentences.feed("<think>Ich ueberlege noch. Und noch mehr.") == []
    assert sentences.flush() == []


def test_sentence_stream_splits_overlong_text_without_punctuation():
    pieces = _stream("wort " * 120)
    assert len(pieces) > 1
    assert all(len(piece) <= 250 for piece in pieces)


# ---- LiteLLM-Client ----------------------------------------------------------------


def _sse(*chunks: dict) -> str:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def _patch_litellm_http(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient

    def client_with_transport(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(litellm_module.httpx, "AsyncClient", client_with_transport)


async def test_stream_chat_message_passes_deltas_and_assembles_tool_calls(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        body = _sse(
            {"choices": [{"delta": {"role": "assistant", "content": "Ich schaue "}}]},
            {"choices": [{"delta": {"content": "nach."}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "get_time", "arguments": '{"zo'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": 'ne": "Berlin"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    _patch_litellm_http(monkeypatch, handler)
    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    message = await litellm_module.litellm_client.stream_chat_message(
        [{"role": "user", "content": "Wie spaet?"}], tools=[{"type": "function"}], on_delta=on_delta
    )

    assert seen["payload"]["stream"] is True
    assert deltas == ["Ich schaue ", "nach."]
    assert message["content"] == "Ich schaue nach."
    assert message["tool_calls"] == [{
        "id": "call_1", "type": "function",
        "function": {"name": "get_time", "arguments": '{"zone": "Berlin"}'},
    }]


async def test_stream_chat_message_raises_with_the_error_body(monkeypatch):
    _patch_litellm_http(monkeypatch, lambda request: httpx.Response(400, text="template error"))

    with pytest.raises(RuntimeError, match="LLM-Fehler 400.*template error"):
        await litellm_module.litellm_client.stream_chat_message([{"role": "user", "content": "x"}])


# ---- Engine bleibt fuer die ganze Antwort -----------------------------------------


async def test_stream_main_reports_the_engine_and_honours_a_fixed_engine(monkeypatch):
    async def piper_stream(text, voice_id, **kwargs):
        yield 22050, b"\x01\x00" * 10

    async def xtts_stream(text, voice_id, **kwargs):
        yield 24000, b"\x02\x00" * 10

    monkeypatch.setattr(piper_client, "stream", piper_stream)
    monkeypatch.setattr(xtts_client, "stream", xtts_stream)
    chosen: list[str] = []

    first = [item async for item in tts_engines.stream_main("Satz eins.", "v", on_engine=chosen.append)]
    later = [item async for item in tts_engines.stream_main("Satz zwei.", "v", engine_id="piper")]

    assert chosen == ["xtts"] and first[0][0] == 24000
    assert later[0][0] == 22050


# ---- WebSocket ---------------------------------------------------------------------


def _open(ws) -> None:
    assert ws.receive_json()["type"] == "session"


def _collect_until_done(ws) -> list[dict]:
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == "done":
            return frames


def _send_audio_input(ws) -> None:
    ws.send_json({"type": "audio_chunk", "data": pcm_to_b64(b"\x00\x00" * 1600)})
    ws.send_json({"type": "audio_end"})


def _patch_streaming(monkeypatch, rounds: list[list], summary: str | None = None) -> dict:
    """rounds: je LLM-Runde eine Liste aus Text-Deltas und Pausen (float,
    Sekunden); eine Runde kann mit {"tool_calls": [...]} enden. Gibt ein
    dict mit gesprochenen Texten und Kurzfassungs-Aufrufen zurueck."""
    record: dict = {"spoken": [], "summary_calls": []}
    calls = {"n": 0}

    async def fake_stream(messages, tools=None, on_delta=None, model=None):
        parts = rounds[min(calls["n"], len(rounds) - 1)]
        calls["n"] += 1
        content, tool_calls = [], None
        for part in parts:
            if isinstance(part, float):
                await asyncio.sleep(part)
            elif isinstance(part, dict):
                tool_calls = part["tool_calls"]
            else:
                content.append(part)
                await on_delta(part)
        message = {"role": "assistant", "content": "".join(content) or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    async def fake_chat(messages, tools=None, model=None):
        record["summary_calls"].append(messages)
        return {"role": "assistant", "content": summary or ""}

    async def capture_xtts(text, voice_id, language=None):
        record["spoken"].append(text)
        yield 24000, b"\x01\x02" * 240

    monkeypatch.setattr(settings, "llm_streaming_enabled", True)
    monkeypatch.setattr(settings, "filler_enabled", False)
    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "stream_chat_message", fake_stream)
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", fake_chat)
    monkeypatch.setattr(stream_module.stt_client, "transcribe", AsyncMock(return_value="Wie wird das Wetter?"))
    monkeypatch.setattr(xtts_client, "stream", capture_xtts)
    return record


def test_text_turn_streams_deltas_into_the_chat(client, monkeypatch):
    _patch_streaming(monkeypatch, [["Hallo ", "Welt", "!"]])

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "text_input", "text": "Hi"})
        frames = _collect_until_done(ws)

    texts = [f for f in frames if f["type"] == "assistant_text"]
    assert texts == [
        {"type": "assistant_text", "text": "Hallo ", "final": False},
        {"type": "assistant_text", "text": "Welt", "final": False},
        {"type": "assistant_text", "text": "!", "final": False},
        {"type": "assistant_text", "text": "Hallo Welt!", "final": True},
    ]
    # Getippt -> weiterhin kein Server-Audio.
    assert not any(f["type"] in ("audio_chunk", "audio_end") for f in frames)


def test_voice_turn_speaks_the_first_sentence_while_the_llm_still_writes(client, monkeypatch):
    record = _patch_streaming(monkeypatch, [[
        "Morgen wird es sonnig. ", 0.4, "Am Abend zieht Regen auf. ", "Nimm einen Schirm mit.",
    ]])
    monkeypatch.setattr(settings, "voice_summary_enabled", False)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        frames = _collect_until_done(ws)

    types = [f["type"] for f in frames]
    final_text = next(i for i, f in enumerate(frames) if f["type"] == "assistant_text" and f["final"])
    # Der erste Satz klingt, bevor das LLM fertig ist.
    assert types.index("audio_chunk") < final_text
    assert record["spoken"] == [
        "Morgen wird es sonnig.", "Am Abend zieht Regen auf.", "Nimm einen Schirm mit.",
    ]
    # audio_end genau einmal, nach dem letzten Audio, done ganz am Ende.
    assert types.count("audio_end") == 1
    assert types.index("audio_end") > max(i for i, t in enumerate(types) if t == "audio_chunk")
    assert types[-1] == "done"
    assert record["summary_calls"] == []


def test_voice_turn_over_the_limit_speaks_live_then_summarizes_the_rest(client, monkeypatch):
    record = _patch_streaming(
        monkeypatch,
        [["Morgen wird es sonnig. ", "Am Abend zieht Regen auf. ", "Am Wochenende wird es kalt. ",
          "Naechste Woche schneit es vielleicht."]],
        summary="Danach wird es kalt und vielleicht schneit es.",
    )
    repos.set_speech_flow(llm_streaming=True, voice_summary=True, max_chars=50)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        frames = _collect_until_done(ws)

    assert record["spoken"] == [
        "Morgen wird es sonnig.", "Am Abend zieht Regen auf.",
        "Danach wird es kalt und vielleicht schneit es.",
    ]
    (summary_call,) = record["summary_calls"]
    # Die Kurzfassung kennt das schon Gesagte und bekommt die ganze Antwort.
    assert "Morgen wird es sonnig. Am Abend zieht Regen auf." in summary_call[0]["content"]
    assert summary_call[1]["content"].endswith("Naechste Woche schneit es vielleicht.")
    # Im Chat steht trotzdem alles.
    final = next(f for f in frames if f["type"] == "assistant_text" and f["final"])
    assert "Am Wochenende wird es kalt." in final["text"]


def test_voice_turn_falls_back_to_the_skipped_sentences_when_the_summary_fails(client, monkeypatch):
    record = _patch_streaming(
        monkeypatch,
        [["Morgen wird es sonnig. ", "Am Abend zieht Regen auf. ", "Am Wochenende wird es kalt."]],
        summary="",
    )
    repos.set_speech_flow(llm_streaming=True, voice_summary=True, max_chars=30)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        _collect_until_done(ws)

    assert record["spoken"] == [
        "Morgen wird es sonnig.", "Am Abend zieht Regen auf.", "Am Wochenende wird es kalt.",
    ]


def test_text_before_a_tool_call_is_spoken_and_replaces_the_tool_filler(client, monkeypatch):
    record = _patch_streaming(monkeypatch, [
        ["Ich schaue kurz im Kalender nach",
         {"tool_calls": [{"id": "c1", "type": "function",
                          "function": {"name": "unbekannt", "arguments": "{}"}}]}],
        ["Morgen hast du keine Termine."],
    ])
    monkeypatch.setattr(settings, "filler_enabled", True)
    play_filler = AsyncMock()
    monkeypatch.setattr(stream_module.StreamSession, "_play_filler", play_filler)
    monkeypatch.setattr(stream_module.filler_service, "select_filler",
                        lambda kind, voice_id, tool_name=None: {"delay_ms": 0, "path": None, "text": "Moment."}
                        if kind == "tool" else None)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        frames = _collect_until_done(ws)

    assert record["spoken"] == ["Ich schaue kurz im Kalender nach", "Morgen hast du keine Termine."]
    play_filler.assert_not_awaited()
    final = next(f for f in frames if f["type"] == "assistant_text" and f["final"])
    assert final["text"] == "Morgen hast du keine Termine."


def test_later_sentences_stay_with_the_engine_of_the_first(client, monkeypatch):
    """Faellt die aktive Engine beim ersten Satz aus, spricht XTTS - und
    zwar auch die weiteren Saetze, statt es jedes Mal neu zu versuchen."""
    record = _patch_streaming(monkeypatch, [["Morgen wird es sonnig. ", "Am Abend zieht Regen auf."]])
    monkeypatch.setattr(settings, "voice_summary_enabled", False)
    piper_calls: list[str] = []

    async def broken_piper(text, voice_id, **kwargs):
        piper_calls.append(text)
        raise httpx.ConnectError("weg")
        yield  # pragma: no cover

    monkeypatch.setattr(piper_client, "stream", broken_piper)
    monkeypatch.setattr(tts_engines, "active_engine", lambda: "piper")

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        _collect_until_done(ws)

    assert piper_calls == ["Morgen wird es sonnig."]
    assert record["spoken"] == ["Morgen wird es sonnig.", "Am Abend zieht Regen auf."]


def test_a_failing_later_sentence_ends_the_speech_but_not_the_turn(client, monkeypatch):
    _patch_streaming(monkeypatch, [["Morgen wird es sonnig. ", "Am Abend zieht Regen auf. ", "Ende der Ansage."]])
    monkeypatch.setattr(settings, "voice_summary_enabled", False)
    calls: list[str] = []

    async def flaky_xtts(text, voice_id, language=None):
        calls.append(text)
        if len(calls) == 2:
            raise RuntimeError("kaputt")
        yield 24000, b"\x01\x02" * 240

    monkeypatch.setattr(xtts_client, "stream", flaky_xtts)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        frames = _collect_until_done(ws)

    assert calls == ["Morgen wird es sonnig.", "Am Abend zieht Regen auf."]
    types = [f["type"] for f in frames]
    assert "error" not in types and types.count("audio_end") == 1 and types[-1] == "done"


def test_interrupt_stops_the_streamed_speech(client, monkeypatch):
    record = _patch_streaming(monkeypatch, [["Morgen wird es sonnig. ", 5.0, "Das kommt nie an."]])

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        while ws.receive_json()["type"] != "audio_chunk":
            pass
        ws.send_json({"type": "interrupt"})
        frames = _collect_until_done(ws)

    assert record["spoken"] == ["Morgen wird es sonnig."]
    assert not any(f["type"] == "assistant_text" and f.get("final") for f in frames)


def test_interrupt_before_the_first_sentence_stops_the_llm(client, monkeypatch):
    """Barge-in, solange das Filler-Rennen noch laeuft (noch kein Satz
    fertig): Vorher lief das LLM weiter und schickte Deltas nach dem done
    (v1.26.1). Jetzt endet der LLM-Stream mit dem Turn."""
    _patch_streaming(monkeypatch, [[]])
    state = {"emitted": 0, "cancelled": False}

    async def endless_stream(messages, tools=None, on_delta=None, model=None):
        try:
            for index in range(100):
                await on_delta(f"wort{index} ")
                state["emitted"] += 1
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return {"role": "assistant", "content": "nie"}

    monkeypatch.setattr(graph_module.litellm_client, "stream_chat_message", endless_stream)
    monkeypatch.setattr(settings, "filler_enabled", True)
    monkeypatch.setattr(stream_module.filler_service, "select_filler",
                        lambda kind, voice_id, tool_name=None: {"delay_ms": 10000, "path": None, "text": "Moment."})

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        while ws.receive_json()["type"] != "assistant_text":
            pass
        ws.send_json({"type": "interrupt"})
        _collect_until_done(ws)
        emitted_at_done = state["emitted"]
        # Nach dem done schreibt das LLM nicht weiter.
        time.sleep(0.3)

    assert state["cancelled"] is True
    assert state["emitted"] == emitted_at_done


# ---- Schalter im Panel ---------------------------------------------------------------


def test_panel_switches_override_the_env_defaults(client, admin_headers, monkeypatch):
    monkeypatch.setattr(settings, "llm_streaming_enabled", True)
    body = client.get("/v1/admin/character", headers=admin_headers).json()
    assert body["llm_streaming"] is True
    assert body["voice_summary_enabled"] is True
    assert body["voice_summary_max_chars"] == settings.voice_summary_max_chars

    response = client.put(
        "/v1/admin/character",
        headers=admin_headers,
        json={"prompt": "Sei nett.", "llm_streaming": False,
              "voice_summary_enabled": False, "voice_summary_max_chars": 400},
    )
    assert response.status_code == 200
    assert response.json()["llm_streaming"] is False
    assert repos.llm_streaming_enabled() is False
    assert repos.voice_summary_enabled() is False
    assert repos.voice_summary_max_chars() == 400

    # Ohne die neuen Felder (aeltere Clients) bleiben sie, wie sie sind.
    client.put("/v1/admin/character", headers=admin_headers, json={"prompt": "Sei nett."})
    assert repos.llm_streaming_enabled() is False and repos.voice_summary_max_chars() == 400


def test_saving_the_prompt_alone_does_not_pin_the_env_defaults(client, admin_headers, monkeypatch):
    client.put("/v1/admin/character", headers=admin_headers, json={"prompt": "Sei nett."})
    monkeypatch.setattr(settings, "llm_streaming_enabled", True)
    monkeypatch.setattr(settings, "voice_summary_max_chars", 123)
    assert repos.llm_streaming_enabled() is True
    assert repos.voice_summary_max_chars() == 123

    negative = client.put("/v1/admin/character", headers=admin_headers,
                          json={"prompt": "x", "voice_summary_max_chars": -1})
    assert negative.status_code == 422


def test_limit_zero_means_unlimited_and_has_no_upper_bound(client, admin_headers):
    """v1.26.1: 0 = unbegrenzt, jede Zahl darueber ist ein Limit ohne
    Obergrenze (vorher 50 bis 5000)."""
    for value in (0, 20000):
        response = client.put("/v1/admin/character", headers=admin_headers,
                              json={"prompt": "x", "voice_summary_max_chars": value})
        assert response.status_code == 200
        assert response.json()["voice_summary_max_chars"] == value
    assert repos.voice_summary_limit() == 20000
    repos.set_speech_flow(max_chars=0)
    assert repos.voice_summary_enabled() is True
    assert repos.voice_summary_limit() is None


def test_limit_zero_speaks_everything_while_streaming(client, monkeypatch):
    sentences = ["Morgen wird es sonnig. ", "Am Abend zieht Regen auf. ", "Am Wochenende wird es kalt. "]
    record = _patch_streaming(monkeypatch, [sentences * 5], summary="Darf nicht kommen.")
    repos.set_speech_flow(llm_streaming=True, voice_summary=True, max_chars=0)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "hello", "mode": "talk"})
        _send_audio_input(ws)
        _collect_until_done(ws)

    assert " ".join(record["spoken"]) == "".join(sentences * 5).strip()
    assert record["summary_calls"] == []


def test_summary_switch_also_applies_without_streaming(client, monkeypatch):
    """Ohne Streaming: Schalter aus -> auch lange Antworten woertlich."""
    long_text = "Sehr ausfuehrliche Antwort. " * 30
    spoken: list[str] = []

    async def fake_chat(messages, tools=None):
        return {"role": "assistant", "content": long_text}

    async def capture_xtts(text, voice_id, language=None):
        spoken.append(text)
        yield 24000, b"\x01\x02" * 100

    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", fake_chat)
    monkeypatch.setattr(stream_module.stt_client, "transcribe", AsyncMock(return_value="Erzaehl"))
    monkeypatch.setattr(xtts_client, "stream", capture_xtts)
    monkeypatch.setattr(settings, "filler_enabled", False)
    # Kurzfassung aus - oder an mit Limit 0 (unbegrenzt): beides woertlich.
    for summary, limit in ((False, 280), (True, 0)):
        spoken.clear()
        repos.set_speech_flow(llm_streaming=False, voice_summary=summary, max_chars=limit)

        with client.websocket_connect("/v1/assistant/stream") as ws:
            _open(ws)
            ws.send_json({"type": "hello", "mode": "talk"})
            _send_audio_input(ws)
            frames = _collect_until_done(ws)

        assert spoken == [long_text]
        assert [f["final"] for f in frames if f["type"] == "assistant_text"] == [True]
