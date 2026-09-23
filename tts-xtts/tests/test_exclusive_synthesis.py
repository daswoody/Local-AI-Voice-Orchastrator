"""Immer nur EINE XTTS-Synthese (v1.16).

coqui-tts legt pro Synthese die Text-Konditionierung am Modell selbst ab
(cached_prefix_emb) und liest sie bei jedem Audio-Token wieder aus. Zwei
verschraenkte Synthesen rechnen deshalb mit Text/Laenge der jeweils anderen
weiter (Zeitlupe, Fetzen am Ende, IndexError). Diese Tests sichern die
Serialisierung ab - mit einem Fake-Modell, ohne torch."""

import threading
import time

import pytest

from tts_xtts import engine as engine_module
from tts_xtts.engine import EngineBusy, XttsEngine


class _GatedModel:
    """Fake-XTTS: liefert pro Text zwei Chunks und haelt dazwischen an, bis
    der Test `gate` fuer diesen Text oeffnet - so laesst sich eine lange
    laufende Generierung nachstellen."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.gates: dict[str, threading.Event] = {}
        self.inference_kwargs: dict | None = None

    def gate(self, text: str) -> threading.Event:
        return self.gates.setdefault(text, threading.Event())

    def inference_stream(self, text, language, gpt_cond_latent, speaker_embedding):
        self.events.append(f"{text}:start")
        yield f"{text}-1".encode()
        self.gate(text).wait(5)
        yield f"{text}-2".encode()
        self.events.append(f"{text}:end")

    def inference(self, text, language, gpt_cond_latent, speaker_embedding, **kwargs):
        self.inference_kwargs = kwargs
        self.events.append(f"{text}:full")
        return {"wav": f"{text}-wav".encode()}


@pytest.fixture
def model(monkeypatch) -> _GatedModel:
    # Tensor-/numpy-Konvertierung ueberbruecken: das Fake-Modell liefert Bytes.
    monkeypatch.setattr(engine_module, "_tensor_to_pcm16", lambda chunk: chunk)
    monkeypatch.setattr(engine_module, "_float_to_pcm16", lambda wav: wav)
    return _GatedModel()


@pytest.fixture
def engine(model, monkeypatch) -> XttsEngine:
    engine = XttsEngine()
    engine._model = model
    engine._effective = "cpu"
    monkeypatch.setattr(engine, "_latents", lambda voice_id: ("gpt", "speaker"))
    return engine


def _consume_in_thread(engine: XttsEngine, text: str) -> tuple[threading.Thread, list]:
    result: list = []

    def run() -> None:
        try:
            result.extend(engine.stream(text, "papa"))
        except Exception as exc:
            result.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_second_synthesis_waits_until_first_is_done(engine, model):
    first = engine.stream("A", "papa")
    assert next(first) == b"A-1"

    thread, result_b = _consume_in_thread(engine, "B")
    time.sleep(0.2)
    # B darf nicht anfangen, solange A noch generiert
    assert "B:start" not in model.events
    assert result_b == []

    model.gate("A").set()
    assert list(first) == [b"A-2"]
    model.gate("B").set()
    thread.join(5)

    assert result_b == [b"B-1", b"B-2"]
    assert model.events == ["A:start", "A:end", "B:start", "B:end"]


def test_full_synthesis_waits_for_running_stream(engine, model):
    stream = engine.stream("A", "papa")
    next(stream)
    done: list = []
    thread = threading.Thread(target=lambda: done.append(engine.synthesize("F", "papa")))
    thread.start()
    time.sleep(0.2)
    assert done == []

    model.gate("A").set()
    list(stream)
    thread.join(5)

    assert done == [b"F-wav"]
    assert model.events == ["A:start", "A:end", "F:full"]


def test_abandoned_stream_frees_engine_when_generation_ends(engine, model):
    """Client bricht ab und der Generator wird NIE geschlossen (so verhaelt
    sich Starlette bei einem Barge-in): Die Sperre darf trotzdem nicht bis
    zum Timeout haengen."""
    abandoned = engine.stream("A", "papa")
    next(abandoned)  # Referenz bleibt bewusst liegen, kein close()

    model.gate("A").set()
    model.gate("B").set()
    started = time.monotonic()
    assert list(engine.stream("B", "papa")) == [b"B-1", b"B-2"]
    assert time.monotonic() - started < 2


def test_closing_stream_stops_generation_early(engine, model):
    stream = engine.stream("A", "papa")
    next(stream)
    stream.close()  # Barge-in mit sauberem close()
    model.gate("A").set()

    model.gate("B").set()
    assert list(engine.stream("B", "papa")) == [b"B-1", b"B-2"]
    # A hat nach dem Abbruch keinen weiteren Chunk mehr zu Ende gerechnet
    assert "A:end" not in model.events


def test_waiting_too_long_raises_engine_busy(engine, model, monkeypatch):
    from tts_xtts.config import settings

    monkeypatch.setattr(settings, "synth_lock_timeout_s", 0.05)
    first = engine.stream("A", "papa")
    next(first)

    with pytest.raises(EngineBusy):
        next(engine.stream("B", "papa"))
    with pytest.raises(EngineBusy):
        engine.synthesize("C", "papa")

    model.gate("A").set()
    list(first)


def test_device_switch_waits_for_running_synthesis(engine, model, monkeypatch):
    """Modell/Latents mitten in einer Generierung auszutauschen, waere
    derselbe Fehler: set_device wartet auf die laufende Synthese."""
    from tts_xtts.config import settings

    monkeypatch.setattr(settings, "synth_lock_timeout_s", 0.05)
    monkeypatch.setattr(engine, "_free_memory", lambda: None)
    monkeypatch.setattr(engine, "_load_locked", lambda: None)
    first = engine.stream("A", "papa")
    next(first)

    with pytest.raises(EngineBusy):
        engine.set_device("cuda:1")

    model.gate("A").set()
    list(first)
    assert engine.set_device("cuda:1")["assigned"] == "cuda:1"


def test_full_synthesis_passes_temperature_only_when_set(engine, model):
    assert engine.synthesize("A", "papa") == b"A-wav"
    assert model.inference_kwargs == {}

    engine.synthesize("A", "papa", temperature=0.5)
    assert model.inference_kwargs == {"temperature": 0.5}


def test_engine_error_reaches_the_consumer(engine, model):
    def broken(text, language, gpt_cond_latent, speaker_embedding):
        raise RuntimeError("CUDA out of memory")
        yield  # pragma: no cover

    model.inference_stream = broken
    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        next(engine.stream("A", "papa"))
    # ... und die Sperre ist danach wieder frei
    del model.inference_stream
    model.gate("B").set()
    assert list(engine.stream("B", "papa")) == [b"B-1", b"B-2"]
