"""Umschaltbares Device (v1.14): Das Admin-Panel weist zur Laufzeit eine
Karte zu; das Modell wird dort neu geladen. Wichtig ist die Trennung von
`assigned` (gewuenscht) und `effective` (tatsaechlich) - nur so ist im
Panel sichtbar, wenn der CPU-Fallback gegriffen hat."""

import pytest

from stt_service.engine import WhisperEngine, parse_device


class _FakeSegment:
    text = "Hallo Welt"


class _FakeInfo:
    language = "de"
    duration = 1.0


class _FakeModel:
    def __init__(self, fail_with: Exception | None = None):
        self.fail_with = fail_with

    def transcribe(self, audio, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        return [_FakeSegment()], _FakeInfo()


@pytest.fixture
def engine(monkeypatch):
    from stt_service.config import settings

    monkeypatch.setattr(settings, "whisper_device", "cuda:0")
    return WhisperEngine()


def test_parse_device_splits_index():
    assert parse_device("cuda:1") == ("cuda", 1)
    assert parse_device("cuda") == ("cuda", 0)
    assert parse_device("cpu") == ("cpu", 0)
    # Unsinniger Index darf nicht crashen
    assert parse_device("cuda:x") == ("cuda", 0)


def test_set_device_reloads_model_on_other_card(engine, monkeypatch):
    created = []
    monkeypatch.setattr(
        engine, "_create_model",
        lambda device, compute_type: created.append((device, compute_type)) or _FakeModel(),
    )

    engine.load()
    assert created == [("cuda:0", "float16")]

    status = engine.set_device("cuda:1")

    assert created[-1] == ("cuda:1", "float16")
    assert status["assigned"] == "cuda:1"
    assert status["effective"] == "cuda:1"
    assert status["loaded"] is True


def test_set_device_accepts_compute_type_for_older_cards(engine, monkeypatch):
    """Pascal-Karten rechnen float16 langsam - der Orchestrator gibt den
    passenden Compute-Type mit."""
    created = []
    monkeypatch.setattr(
        engine, "_create_model",
        lambda device, compute_type: created.append((device, compute_type)) or _FakeModel(),
    )

    engine.set_device("cuda:1", compute_type="int8_float16")

    assert created[-1] == ("cuda:1", "int8_float16")
    assert engine.status()["compute_type"] == "int8_float16"


def test_set_device_to_cpu_uses_int8(engine, monkeypatch):
    created = []
    monkeypatch.setattr(
        engine, "_create_model",
        lambda device, compute_type: created.append((device, compute_type)) or _FakeModel(),
    )

    status = engine.set_device("cpu")

    assert created[-1] == ("cpu", "int8")
    assert status["effective"] == "cpu"


def test_status_shows_deviation_after_cpu_fallback(engine, monkeypatch):
    """Karte voll -> CPU-Fallback. Die Zuweisung bleibt aber stehen, damit
    das Panel "zugewiesen: cuda:1 / laeuft auf: cpu" anzeigen kann."""
    def fake_create(device, compute_type):
        if device.startswith("cuda"):
            raise RuntimeError("CUDA failed with out of memory")
        return _FakeModel()

    monkeypatch.setattr(engine, "_create_model", fake_create)

    status = engine.set_device("cuda:1")

    assert status["assigned"] == "cuda:1"
    assert status["effective"] == "cpu"


def test_forced_cpu_survives_further_loads_until_reassigned(engine, monkeypatch):
    """Nach einem CUDA-Fehler in der Inferenz bleibt die Engine auf CPU -
    sonst wuerde sie bei jedem Turn erneut ins volle VRAM laufen."""
    cuda_model = _FakeModel(fail_with=RuntimeError("CUBLAS_STATUS_ALLOC_FAILED"))
    monkeypatch.setattr(
        engine, "_create_model",
        lambda device, compute_type: cuda_model if device.startswith("cuda") else _FakeModel(),
    )

    assert engine.transcribe(b"\x00\x00" * 1600)["device"] == "cpu"
    # Zweiter Turn: weiterhin CPU, kein erneuter CUDA-Versuch
    assert engine.transcribe(b"\x00\x00" * 1600)["device"] == "cpu"

    # Explizite Neuzuweisung hebt die Sperre auf
    monkeypatch.setattr(engine, "_create_model", lambda device, compute_type: _FakeModel())
    assert engine.set_device("cuda:0")["effective"] == "cuda:0"


def test_device_endpoints(monkeypatch):
    """GET/POST /v1/device - die Schnittstelle, die der Orchestrator nutzt."""
    from fastapi.testclient import TestClient

    from stt_service import engine as engine_module
    from stt_service.main import app

    class _FakeEngine:
        def __init__(self):
            self.assigned = "cuda:0"

        def status(self):
            return {"assigned": self.assigned, "effective": self.assigned, "loaded": True}

        def set_device(self, device, compute_type=None):
            self.assigned = device
            return {"assigned": device, "effective": device, "loaded": True,
                    "compute_type": compute_type}

    monkeypatch.setattr(engine_module, "engine", _FakeEngine())
    client = TestClient(app)

    assert client.get("/v1/device").json()["assigned"] == "cuda:0"

    response = client.post("/v1/device", json={"device": "cuda:1", "compute_type": "float16"})
    assert response.status_code == 200
    assert response.json()["effective"] == "cuda:1"
    assert client.get("/v1/device").json()["assigned"] == "cuda:1"

    # Unsinnige Devices werden abgewiesen, bevor irgendetwas geladen wird
    assert client.post("/v1/device", json={"device": "gpu5"}).status_code == 422
