"""Umschaltbares Device (v1.14): XTTS laedt sein Modell auf der vom
Admin-Panel gewaehlten Karte neu. Kritisch dabei: die gecachten
Conditioning-Latents liegen auf der ALTEN Karte und muessen mit verworfen
werden - sonst knallt die naechste Synthese mit einem Device-Mismatch."""

import pytest
from fastapi.testclient import TestClient

from tts_xtts.engine import XttsEngine


class _FakeTtsModel:
    def __init__(self, device: str):
        self.device = device


class _FakeApi:
    """Ahmt TTS(...).to(device) nach, ohne torch/coqui zu brauchen."""

    def __init__(self, loaded: list, fail_on: set[str]):
        self._loaded = loaded
        self._fail_on = fail_on

    def to(self, device: str):
        if device in self._fail_on:
            raise RuntimeError(f"CUDA out of memory auf {device}")
        self._loaded.append(device)
        self.synthesizer = type("S", (), {"tts_model": _FakeTtsModel(device)})()
        return self


@pytest.fixture
def engine(monkeypatch):
    from tts_xtts.config import settings

    monkeypatch.setattr(settings, "device", "cuda:0")
    return XttsEngine()


def _patch_loader(engine, monkeypatch, fail_on: set[str] | None = None) -> list:
    loaded: list[str] = []

    def fake_load_locked():
        if engine._model is not None:
            return
        api = _FakeApi(loaded, fail_on or set())
        target = engine._assigned
        try:
            api.to(target)
        except Exception:
            if target == "cpu":
                raise
            api.to("cpu")
            target = "cpu"
        engine._model = api.synthesizer.tts_model
        engine._effective = target

    monkeypatch.setattr(engine, "_load_locked", fake_load_locked)
    monkeypatch.setattr(engine, "_free_memory", lambda: None)
    return loaded


def test_set_device_reloads_on_other_card(engine, monkeypatch):
    loaded = _patch_loader(engine, monkeypatch)

    engine.load()
    assert loaded == ["cuda:0"]

    status = engine.set_device("cuda:1")

    assert loaded == ["cuda:0", "cuda:1"]
    assert status["assigned"] == "cuda:1"
    assert status["effective"] == "cuda:1"


def test_set_device_clears_latents_cache(engine, monkeypatch):
    """Latents sind GPU-Tensoren der alten Karte - beim Wechsel weg damit."""
    _patch_loader(engine, monkeypatch)
    engine.load()
    engine._latents_cache["papa"] = (123, ("latent", "embedding"))

    engine.set_device("cuda:1")

    assert engine._latents_cache == {}


def test_full_card_falls_back_to_cpu_and_reports_deviation(engine, monkeypatch):
    _patch_loader(engine, monkeypatch, fail_on={"cuda:1"})

    status = engine.set_device("cuda:1")

    assert status["assigned"] == "cuda:1"
    assert status["effective"] == "cpu"


def test_off_unloads_the_model_and_blocks_synthesis(engine, monkeypatch):
    """Aus (v1.19): Modell und Latents weg, keine Synthese mehr - bis wieder
    eine Karte zugewiesen wird."""
    from tts_xtts.engine import EngineDisabled

    loaded = _patch_loader(engine, monkeypatch)
    engine.load()
    engine._latents_cache["papa"] = (123, ("latent", "embedding"))

    status = engine.set_device("off")

    assert status == {"assigned": "off", "effective": None, "loaded": False,
                      "model": status["model"]}
    assert engine._latents_cache == {}
    with pytest.raises(EngineDisabled, match="ausgeschaltet"):
        engine.load()
    with pytest.raises(EngineDisabled):
        engine.synthesize("Hallo", "papa")
    assert loaded == ["cuda:0"]

    assert engine.set_device("cuda:1")["effective"] == "cuda:1"
    assert loaded == ["cuda:0", "cuda:1"]


def test_device_endpoints(monkeypatch):
    from tts_xtts import engine as engine_module
    from tts_xtts.main import app

    class _FakeEngine:
        def __init__(self):
            self.assigned = "cuda:0"

        def status(self):
            return {"assigned": self.assigned, "effective": self.assigned, "loaded": True}

        def set_device(self, device):
            self.assigned = device
            return {"assigned": device, "effective": device, "loaded": True}

    monkeypatch.setattr(engine_module, "engine", _FakeEngine())
    client = TestClient(app)

    assert client.get("/v1/device").json()["assigned"] == "cuda:0"
    assert client.post("/v1/device", json={"device": "cuda:1"}).json()["effective"] == "cuda:1"
    assert client.post("/v1/device", json={"device": "off"}).json()["assigned"] == "off"
    assert client.post("/v1/device", json={"device": "quatsch"}).status_code == 422
