"""Engine-Vertrag v1 gegen ein Fake-Backend in einem echten Unterprozess."""

import json
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from tts_engine_kit import create_app
from tts_engine_kit.worker import WorkerSettings

BACKEND = "fake_backend:FakeBackend"


def _settings(**overrides) -> WorkerSettings:
    values = {"device": "cpu", "preload": False, "busy_wait_s": 20.0, "load_wait_s": 20.0,
              "chunk_timeout_s": 20.0, "load_retry_s": 60.0}
    values.update(overrides)
    return WorkerSettings(**values)


@pytest.fixture
def client():
    app = create_app(BACKEND, _settings())
    with TestClient(app) as test_client:
        _wait_state(test_client, {"idle"})
        yield test_client


def _wait_state(client, states: set, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/v1/device").json()
        if status["state"] in states:
            return status
        time.sleep(0.05)
    raise AssertionError(f"Zustand {states} nicht erreicht: {status}")


def _lines(body: bytes) -> list[dict]:
    return [json.loads(line) for line in body.split(b"\n") if line.strip()]


def _speak(client, text="Hallo Welt. Wie geht es dir heute so?", **fields):
    data = {"text": text, "language": "de", "load_timeout_s": "20", **fields}
    return client.post("/v1/synthesize", data=data)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ---- Steckbrief und Geraet ------------------------------------------------------------


def test_info_health_and_initial_state(client):
    info = client.get("/v1/info").json()
    assert info["name"] == "Fake-TTS" and info["languages"] == ["de", "en"]
    assert info["contract"] == 1 and info["sample_rate"] == 16000
    assert client.get("/v1/health").json() == {"status": "ok", "state": "idle"}
    status = client.get("/v1/device").json()
    assert status["assigned"] == "cpu" and status["loaded"] is False


def test_device_off_ends_the_model_process(client):
    """Entladen = Prozess beenden: danach haelt nichts mehr VRAM."""
    status = client.post("/v1/device", json={"device": "cpu"}).json()
    assert status["state"] == "ready" and status["effective"] == "cpu"
    pid = _lines(_speak(client).content)[0]["pid"]
    assert pid != os.getpid() and _alive(pid)

    status = client.post("/v1/device", json={"device": "off"}).json()

    assert status["state"] == "off" and status["loaded"] is False
    time.sleep(0.2)
    assert not _alive(pid)
    response = _speak(client)
    assert response.status_code == 503 and "ausgeschaltet" in response.json()["detail"]
    assert client.post("/v1/device", json={"device": "gpu5"}).status_code == 422


# ---- Synthese -------------------------------------------------------------------------------


def test_speaks_sentence_by_sentence(client):
    response = _speak(client, "Erster Satz ist hier. Zweiter Satz folgt sogleich! Und hier kommt der dritte?",
                      instruction="ruhig")

    assert response.status_code == 200
    assert response.headers["x-sample-rate"] == "16000"
    lines = _lines(response.content)
    assert [line["s"] for line in lines] == [
        "Erster Satz ist hier.", "Zweiter Satz folgt sogleich!", "Und hier kommt der dritte?"]
    assert lines[0]["lang"] == "de" and lines[0]["instr"] == "ruhig"
    assert lines[0]["tag"] == "vorbereitet"  # prepare() -> Konstruktor im Unterprozess


def test_unloaded_engine_answers_503_at_once_and_loads_meanwhile(client, monkeypatch):
    """Live-Turn (load_timeout 0): nicht warten - der Orchestrator nimmt so
    lange seine Rueckfallebene; das Laden laeuft trotzdem an."""
    monkeypatch.setenv("FAKE_LOAD_S", "0.5")

    response = _speak(client, load_timeout_s="0")

    assert response.status_code == 503 and "geladen" in response.json()["detail"]
    _wait_state(client, {"ready"})
    assert _speak(client, load_timeout_s="0").status_code == 200


def test_errors_before_the_first_audio_are_clean_http_errors(client):
    broken = _speak(client, "boom und weiter")
    assert broken.status_code == 500 and "Modell kaputt" in broken.json()["detail"]
    assert _speak(client, language="xx").status_code == 400
    assert _speak(client, "   ").status_code == 400
    assert _speak(client).status_code == 200  # Engine bleibt nutzbar


def test_crashed_model_process_is_reported_and_reloaded(client):
    first_pid = _lines(_speak(client).content)[0]["pid"]

    crashed = _speak(client, "crash jetzt")

    assert crashed.status_code == 500 and "unerwartet beendet" in crashed.json()["detail"]
    status = client.get("/v1/device").json()
    assert status["state"] == "error" and "Exit-Code 3" in status["detail"]
    again = _speak(client)
    assert again.status_code == 200
    assert _lines(again.content)[0]["pid"] != first_pid


def test_reference_is_prepared_once_per_sample_and_transcript(client):
    wav = b"RIFF" + b"\x01" * 100
    ref = {"ref_audio": ("stimme.wav", wav, "audio/wav")}

    def speak(transcript):
        response = client.post("/v1/synthesize", files=ref, data={
            "text": "Ein kurzer Testsatz fuer den Cache.", "ref_text": transcript,
            "load_timeout_s": "20"})
        return _lines(response.content)[0]

    first = speak("Das ist mein Sample.")
    second = speak("Das ist mein Sample.")
    third = speak("Ein anderes Transkript.")

    assert first["voice"] == {"transcript": "Das ist mein Sample.", "bytes": len(wav)}
    assert (first["prep"], second["prep"], third["prep"]) == (1, 1, 2)


def test_second_request_waits_for_the_running_one(client, monkeypatch):
    monkeypatch.setenv("FAKE_SENTENCE_S", "0.3")
    client.post("/v1/device", json={"device": "cpu"})  # Env erst ab jetzt im Prozess
    results = []

    def speak():
        results.append(_speak(client, "Satz eins ist da. Satz zwei ist auch da.").status_code)

    threads = [threading.Thread(target=speak) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert results == [200, 200]


def test_busy_engine_answers_409_after_the_wait(monkeypatch):
    monkeypatch.setenv("FAKE_SENTENCE_S", "0.5")
    app = create_app(BACKEND, _settings(busy_wait_s=0.2))
    with TestClient(app) as client:
        _wait_state(client, {"idle"})
        client.post("/v1/device", json={"device": "cpu"})
        results = []
        slow = threading.Thread(target=lambda: results.append(
            _speak(client, "Satz eins ist da. Satz zwei ist da. Satz drei ist da.").status_code))
        slow.start()
        time.sleep(0.3)
        busy = _speak(client)
        slow.join(30)

    assert busy.status_code == 409 and "belegt" in busy.json()["detail"]
    assert results == [200]


def test_load_failure_names_the_reason_and_is_not_retried_every_request(client, monkeypatch):
    monkeypatch.setenv("FAKE_LOAD_FAIL", "1")

    status = client.post("/v1/device", json={"device": "cpu"}).json()

    assert status["state"] == "error" and "CUDA out of memory" in status["detail"]
    response = _speak(client, load_timeout_s="0")
    assert response.status_code == 503 and "CUDA out of memory" in response.json()["detail"]
    # Ein erneuter Geraetewechsel versucht es sofort wieder.
    monkeypatch.delenv("FAKE_LOAD_FAIL")
    assert client.post("/v1/device", json={"device": "cpu"}).json()["state"] == "ready"


def test_failed_preparation_is_reported(monkeypatch):
    monkeypatch.setenv("FAKE_PREPARE_FAIL", "1")
    app = create_app(BACKEND, _settings())
    with TestClient(app) as client:
        status = _wait_state(client, {"error"})
        assert "Vorbereitung fehlgeschlagen" in status["detail"]
        response = _speak(client, load_timeout_s="0")
        assert response.status_code == 503 and "Download kaputt" in response.json()["detail"]


def test_preload_loads_right_after_the_start():
    app = create_app(BACKEND, _settings(preload=True))
    with TestClient(app) as client:
        assert _wait_state(client, {"ready"})["effective"] == "cpu"


# ---- Abbruch durch den Client (echter Server, TestClient puffert Antworten) ----------------


@pytest.fixture
def live_url(monkeypatch):
    monkeypatch.setenv("FAKE_SENTENCE_S", "0.25")
    app = create_app(BACKEND, _settings(busy_wait_s=10.0))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            if httpx.get(f"{url}/v1/health").json()["state"] == "idle":
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    yield url
    server.should_exit = True
    thread.join(20)


def test_client_disconnect_stops_the_synthesis(live_url):
    long_text = " ".join(f"Das ist der Satz Nummer {i} in dieser Reihe." for i in range(20))
    data = {"text": long_text, "load_timeout_s": "20"}
    with httpx.stream("POST", f"{live_url}/v1/synthesize", data=data, timeout=30) as response:
        assert response.status_code == 200
        next(response.iter_bytes())
    # Abgebrochen nach ~1 Satz statt 20 x 0,25 s: der naechste Request ist schnell dran.
    started = time.monotonic()
    follow_up = httpx.post(f"{live_url}/v1/synthesize", data={"text": "Kurz und gut, fertig.",
                                                             "load_timeout_s": "20"}, timeout=30)
    assert follow_up.status_code == 200
    assert time.monotonic() - started < 3.0
