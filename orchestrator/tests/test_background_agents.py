"""Hintergrund-Agenten (v1.29): Agenten laufen neben dem Gespraech weiter,
ihr Ergebnis fliesst in eine spaetere Antwort ein - oder die Assistenz
meldet sich damit von selbst, wenn der Nutzer nichts mehr sagt."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

from orchestrator import graph as graph_module, repos
from orchestrator.config import settings


@pytest.fixture(autouse=True)
def background_on(monkeypatch):
    monkeypatch.setattr(settings, "agent_background_enabled", True)
    # Proaktive Meldung nur in den Tests, die sie pruefen.
    monkeypatch.setattr(settings, "agent_result_idle_s", 0.0)
    monkeypatch.setattr(graph_module.weaviate_client, "search", AsyncMock(return_value=[]))
    repos.create_agent("routen", "Routenplanung", "Plant Reiserouten.", "", "cloud/route")


def _open(ws) -> None:
    assert ws.receive_json()["type"] == "session"
    ws.send_json({"type": "hello", "mode": "chat"})


def _until_done(ws) -> list[dict]:
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == "done":
            return frames


def _tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


def _final_text(frames: list[dict]) -> str:
    return next(f["text"] for f in frames if f["type"] == "assistant_text" and f["final"])


class FakeLlm:
    """Haupt-LLM und Agent in einem: der Agent (Modell cloud/route) wartet,
    bis der Test ihn freigibt; das Haupt-LLM ruft bei "Berlin" den Agenten
    und antwortet sonst mit einer festen Zeile je Nutzereingabe."""

    def __init__(self, answers: dict[str, str], gated: bool = True) -> None:
        self.answers = answers
        self.release = threading.Event()
        if not gated:
            self.release.set()
        self.main_calls: list[list[dict]] = []
        self.agent_runs = 0

    async def __call__(self, messages, tools=None, model=None, think=None):
        if model == "cloud/route":
            self.agent_runs += 1
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            return {"role": "assistant", "content": "ICE 9:00 ab Hamburg, 1:45 h; Auto 3 h."}
        self.main_calls.append([dict(m) for m in messages])
        question = messages[-1]["content"] if messages[-1]["role"] != "tool" else None
        if question is not None and "Berlin" in str(question):
            return _tool_call("agent-routen", {"task": "Route Hamburg-Berlin"})
        if question is None:
            return {"role": "assistant", "content": "Ich suche die passenden Infos raus."}
        return {"role": "assistant", "content": self.answers.get(str(question), "Ok.")}

    def system_prompt(self, call: int) -> str:
        return self.main_calls[call][0]["content"]


def test_agent_runs_in_background_and_result_flows_into_a_later_answer(client, monkeypatch):
    """Der Ablauf aus dem Nutzerwunsch: Agent starten, weiterreden, das
    Ergebnis kommt rein und die naechste Antwort baut es ein."""
    llm = FakeLlm({
        "Vielen Dank.": "Sehr gerne. Was hast du denn in Berlin vor?",
        "Ich besuche eine Messe.": "Das ist cool. Ich habe gerade passende Routen gefunden.",
    })
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", llm)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "text_input", "text": "Wie komme ich am besten nach Berlin?"})
        first = _until_done(ws)
        conversation_id = next(f for f in first if f["type"] == "conversation")["conversation_id"]

        # Die Antwort wartet NICHT auf den Agenten.
        started = [f for f in first if f["type"] == "tool_activity"]
        assert started == [{"type": "tool_activity", "tool": "agent-routen",
                            "status": "background", "job_id": started[0]["job_id"]}]
        job_id = started[0]["job_id"]
        assert _final_text(first) == "Ich suche die passenden Infos raus."
        tool_message = next(m for m in llm.main_calls[1] if m["role"] == "tool")
        assert "laeuft im Hintergrund" in tool_message["content"]

        ws.send_json({"type": "text_input", "text": "Vielen Dank."})
        second = _until_done(ws)
        assert _final_text(second) == "Sehr gerne. Was hast du denn in Berlin vor?"
        # Das Haupt-LLM weiss, dass der Agent noch laeuft.
        assert "agent-routen (Aufgabe: Route Hamburg-Berlin" in llm.system_prompt(2)
        assert "Neue Ergebnisse" not in llm.system_prompt(2)

        # Agent wird fertig - ausserhalb jedes Turns.
        llm.release.set()
        assert ws.receive_json() == {"type": "tool_activity", "tool": "agent-routen",
                                     "status": "done", "job_id": job_id}

        ws.send_json({"type": "text_input", "text": "Ich besuche eine Messe."})
        third = _until_done(ws)
        assert _final_text(third).startswith("Das ist cool.")
        prompt = llm.system_prompt(3)
        assert "Neue Ergebnisse von Hintergrund-Agenten" in prompt
        assert "ICE 9:00 ab Hamburg" in prompt
        assert "Hintergrund-Agenten, die gerade" not in prompt

        # Das Ergebnis wird genau einmal zugestellt, bleibt aber im Verlauf.
        ws.send_json({"type": "text_input", "text": "Und zurueck?"})
        _until_done(ws)
        assert "Neue Ergebnisse" not in llm.system_prompt(4)
        history = "\n".join(str(m["content"]) for m in llm.main_calls[4][1:])
        assert "[Ergebnis vom Hintergrund-Agenten agent-routen" in history

    assert llm.agent_runs == 1
    messages = repos.list_messages(conversation_id)
    # Chip des Start-Turns bekommt den Endstatus auch in der Historie.
    assert messages[1]["tools"] == [{"tool": "agent-routen", "status": "done", "job_id": job_id}]
    assert messages[5]["agent_results"] == [{
        "tool": "agent-routen", "task": "Route Hamburg-Berlin", "status": "done",
        "result": "ICE 9:00 ab Hamburg, 1:45 h; Auto 3 h.",
    }]


def test_assistant_reports_the_result_by_itself_when_the_user_stays_silent(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_result_idle_s", 0.05)
    llm = FakeLlm({}, gated=False)

    async def chat(messages, tools=None, model=None, think=None):
        if model is None and "[Hinweis des Systems" in str(messages[-1]["content"]):
            llm.main_calls.append([dict(m) for m in messages])
            return {"role": "assistant", "content": "Ich habe uebrigens deine Routen."}
        return await llm(messages, tools, model, think)

    monkeypatch.setattr(graph_module.litellm_client, "chat_message", chat)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "text_input", "text": "Wie komme ich nach Berlin?"})
        first = _until_done(ws)
        conversation_id = next(f for f in first if f["type"] == "conversation")["conversation_id"]
        # Ohne weitere Eingabe: Agent fertig, dann meldet sich die Assistenz.
        unsolicited = _until_done(ws)

    job_id = next(f["job_id"] for f in first if f["type"] == "tool_activity")
    assert {"type": "tool_activity", "tool": "agent-routen", "status": "done",
            "job_id": job_id} in first + unsolicited
    assert _final_text(unsolicited) == "Ich habe uebrigens deine Routen."
    proactive_call = llm.main_calls[-1]
    assert "Teile dem Nutzer das Ergebnis jetzt mit." in proactive_call[0]["content"]
    # Keine erfundene Nutzernachricht in der Historie.
    roles = [m["role"] for m in repos.list_messages(conversation_id)]
    assert roles == ["user", "assistant", "assistant"]


def test_consecutive_assistant_messages_are_merged_in_the_history(client, monkeypatch):
    """Nach einer proaktiven Meldung stehen zwei Antworten hintereinander -
    strenge Chat-Templates verlangen user/assistant im Wechsel."""
    seen = {}

    async def chat(messages, tools=None, model=None, think=None):
        seen["messages"] = [dict(m) for m in messages]
        return {"role": "assistant", "content": "Ok."}

    monkeypatch.setattr(graph_module.litellm_client, "chat_message", chat)
    conversation = repos.create_conversation("admin", "pytest", title="x")
    repos.append_message(conversation["id"], "user", "Route?")
    repos.append_message(conversation["id"], "assistant", "Ich schaue.")
    repos.append_message(conversation["id"], "assistant", "Hier ist sie.")

    login = client.post("/v1/auth/login", json={
        "username": settings.admin_username, "password": settings.admin_password,
        "device_name": "pytest"})
    token = login.json()["token"]
    with client.websocket_connect(f"/v1/assistant/stream?token={token}") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "hello", "mode": "chat", "conversation_id": conversation["id"]})
        ws.send_json({"type": "text_input", "text": "Danke"})
        _until_done(ws)

    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert seen["messages"][2]["content"] == "Ich schaue.\nHier ist sie."


def test_same_task_is_not_started_twice(client, monkeypatch):
    llm = FakeLlm({})

    async def chat(messages, tools=None, model=None, think=None):
        if model is None:
            tool_rounds = sum(1 for m in messages if m["role"] == "tool")
            if tool_rounds < 2:
                return _tool_call("agent-routen", {"task": "Route Hamburg-Berlin"},
                                  call_id=f"call_{tool_rounds}")
            llm.main_calls.append([dict(m) for m in messages])
            return {"role": "assistant", "content": "Laeuft."}
        return await llm(messages, tools, model, think)

    monkeypatch.setattr(graph_module.litellm_client, "chat_message", chat)
    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "text_input", "text": "Berlin?"})
        frames = _until_done(ws)
        llm.release.set()

    assert len([f for f in frames if f["type"] == "tool_activity"]) == 1
    tool_results = [m["content"] for m in llm.main_calls[0] if m["role"] == "tool"]
    assert "laeuft bereits" in tool_results[1]


def test_over_the_limit_the_agent_runs_inside_the_turn(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_background_max", 0)
    llm = FakeLlm({}, gated=False)
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", llm)

    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "text_input", "text": "Wie komme ich nach Berlin?"})
        frames = _until_done(ws)

    statuses = [f["status"] for f in frames if f["type"] == "tool_activity"]
    assert statuses == ["running", "done"]
    tool_message = next(m for m in llm.main_calls[1] if m["role"] == "tool")
    assert tool_message["content"] == "ICE 9:00 ab Hamburg, 1:45 h; Auto 3 h."


def test_switch_in_the_panel_turns_background_off(client, admin_headers, monkeypatch):
    response = client.put("/v1/admin/agent-background",
                          json={"enabled": False, "idle_s": 30}, headers=admin_headers)
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "idle_s": 30.0,
                               "max_parallel": settings.agent_background_max}
    assert client.get("/v1/admin/agent-background", headers=admin_headers).json()["idle_s"] == 30.0

    llm = FakeLlm({}, gated=False)
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", llm)
    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "text_input", "text": "Wie komme ich nach Berlin?"})
        frames = _until_done(ws)

    assert [f["status"] for f in frames if f["type"] == "tool_activity"] == ["running", "done"]


def test_disconnect_cancels_running_agents_and_marks_their_chip(client, monkeypatch):
    llm = FakeLlm({})
    monkeypatch.setattr(graph_module.litellm_client, "chat_message", llm)
    with client.websocket_connect("/v1/assistant/stream") as ws:
        _open(ws)
        ws.send_json({"type": "text_input", "text": "Wie komme ich nach Berlin?"})
        frames = _until_done(ws)
        conversation_id = next(f for f in frames if f["type"] == "conversation")["conversation_id"]

    llm.release.set()
    chips = repos.list_messages(conversation_id)[1]["tools"]
    assert [chip["status"] for chip in chips] == ["error"]
