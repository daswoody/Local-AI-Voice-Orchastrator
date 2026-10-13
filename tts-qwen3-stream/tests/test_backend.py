"""Streaming-Backend gegen den faster_qwen3_tts-Nachbau: Download, dtype,
Klon, Chunks."""

import json
import time

import numpy as np
import pytest
from conftest import wav_bytes
from fastapi.testclient import TestClient

from tts_engine_kit import Voice, create_app
from tts_engine_kit.worker import WorkerSettings
from tts_qwen3_stream import backend


def _calls(kind: str) -> list[dict]:
    import faster_qwen3_tts

    return [call for call in faster_qwen3_tts.CALLS if call["call"] == kind]


def _loaded(device: str = "cuda:0") -> backend.Qwen3StreamBackend:
    engine = backend.Qwen3StreamBackend(model_dir="/models/qwen3")
    engine.load(device)
    return engine


# ---- Steckbrief und Gewichte ------------------------------------------------------------


def test_info_describes_a_streaming_german_capable_clone_engine(monkeypatch):
    info = backend.Qwen3StreamBackend.info()
    assert "de" in info.languages and info.sample_rate == 24000
    assert info.needs_sample and info.uses_transcript and info.streaming == "chunk"
    assert info.name == "Qwen3-TTS 0.6B Streaming" and info.vram_mb == 3500
    monkeypatch.setattr(backend, "MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
    assert backend.Qwen3StreamBackend.info().vram_mb == 6500


def test_weights_are_downloaded_once_into_the_volume(monkeypatch, tmp_path):
    import huggingface_hub

    downloads = []

    def fake_download(repo_id, local_dir, token=None):
        downloads.append((repo_id, local_dir, token))

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_download)
    monkeypatch.setattr(backend, "MODELS_DIR", tmp_path)
    monkeypatch.setenv("HF_TOKEN", "hf_test")

    first = backend.Qwen3StreamBackend.prepare()
    second = backend.Qwen3StreamBackend.prepare()

    target = str(tmp_path / "Qwen--Qwen3-TTS-12Hz-0.6B-Base")
    assert first == second == {"model_dir": target}
    assert downloads == [("Qwen/Qwen3-TTS-12Hz-0.6B-Base", target, "hf_test")]


def test_own_weights_in_the_volume_are_used_as_they_are(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "MODEL", str(tmp_path))
    assert backend.Qwen3StreamBackend.prepare() == {"model_dir": str(tmp_path)}


@pytest.mark.parametrize(("name", "value", "message"), [
    ("DTYPE", "int4", "QWEN3_DTYPE"),
    ("CHUNK_STEPS", 0, "QWEN3_CHUNK_STEPS"),
])
def test_invalid_settings_are_rejected_before_anything_loads(monkeypatch, name, value, message):
    monkeypatch.setattr(backend, name, value)
    with pytest.raises(ValueError, match=message):
        backend.Qwen3StreamBackend.prepare()


# ---- Laden ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("device", "capability", "expected"), [
    ("cuda:1", "7.5", "torch.float16"),   # 2080 Ti: kein natives bfloat16
    ("cuda", "8.6", "torch.bfloat16"),
])
def test_dtype_follows_the_card_and_graphs_are_captured_on_load(monkeypatch, device, capability, expected):
    monkeypatch.setenv("FAKE_CUDA_CAPABILITY", capability)
    _loaded(device)
    assert _calls("from_pretrained")[-1] == {
        "call": "from_pretrained", "path": "/models/qwen3", "device": device, "dtype": expected,
        "attn": "sdpa", "max_seq_len": 2048}
    # Warmup gehoert zum Laden - nicht in den ersten Live-Satz
    assert len(_calls("warmup")) == 1


def test_without_cuda_graphs_nothing_is_captured_and_the_slow_path_is_used(monkeypatch):
    monkeypatch.setattr(backend, "CUDA_GRAPHS", False)
    engine = _loaded()
    voice = engine.prepare_voice(Voice(key="a", wav=wav_bytes(), transcript=None))
    list(engine.synthesize("Ein ganz normaler Satz hier.", "de", voice, None))
    assert _calls("warmup") == []
    assert engine._model._warmed_up is True  # sonst holt das Paket die Aufnahme beim ersten Satz nach
    assert _calls("generate")[0]["parity"] is True


def test_graphs_are_on_by_default():
    engine = _loaded()
    voice = engine.prepare_voice(Voice(key="a", wav=wav_bytes(), transcript=None))
    list(engine.synthesize("Ein ganz normaler Satz hier.", "de", voice, None))
    assert _calls("generate")[0]["parity"] is False


def test_dtype_can_be_forced(monkeypatch):
    monkeypatch.setattr(backend, "DTYPE", "float32")
    _loaded("cuda:0")
    assert _calls("from_pretrained")[-1]["dtype"] == "torch.float32"


def test_cpu_is_refused_with_a_hint_to_audiocpp():
    engine = backend.Qwen3StreamBackend(model_dir="/models/qwen3")
    with pytest.raises(RuntimeError, match="Nvidia-Karte.*audio.cpp"):
        engine.load("cpu")


# ---- Klonen und Sprechen ------------------------------------------------------------------


def test_transcript_selects_the_closer_icl_clone_and_ends_with_silence():
    engine = _loaded()
    engine.prepare_voice(Voice(key="a", wav=wav_bytes(width=3, channels=2), transcript=" Mein Sample. "))
    engine.prepare_voice(Voice(key="b", wav=wav_bytes(), transcript=None))

    icl, xvec = _calls("prompt")
    assert icl["ref_text"] == "Mein Sample." and icl["x_vector_only_mode"] is False
    # 0,5 s Sample + 0,5 s Stille; die Stille verhindert, dass der letzte Laut des Samples
    # in den Anfang der Sprache blutet
    assert icl["rate"] == 24000 and icl["samples"] == 12000 + 12000
    assert icl["peak"] == pytest.approx(0.5, abs=0.01) and icl["tail_peak"] == 0.0
    # ohne Transkript nur der Stimm-Fingerabdruck - dort bleibt das Sample, wie es ist
    assert xvec["ref_text"] is None and xvec["x_vector_only_mode"] is True and xvec["samples"] == 12000


def test_silence_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(backend, "REF_SILENCE_S", 0)
    _loaded().prepare_voice(Voice(key="a", wav=wav_bytes(), transcript="Mein Sample."))
    assert _calls("prompt")[0]["samples"] == 12000


def test_streams_chunks_sentence_by_sentence_in_the_requested_language():
    engine = _loaded()
    voice = engine.prepare_voice(Voice(key="a", wav=wav_bytes(), transcript="Mein Sample."))

    chunks = list(engine.synthesize("Guten Morgen, wie geht es dir? Heute scheint die Sonne.",
                                    "de", voice, None))

    assert [rate for rate, _ in chunks] == [24000] * 4  # 2 Saetze x 2 Chunks
    assert all(len(pcm) == 2400 * 2 for _, pcm in chunks)
    calls = _calls("generate")
    assert [call["text"] for call in calls] == ["Guten Morgen, wie geht es dir?",
                                                "Heute scheint die Sonne."]
    assert {call["language"] for call in calls} == {"german"}
    assert {call["chunk_size"] for call in calls} == {8}
    assert calls[0]["prompt"] == [{"prompt": "Mein Sample."}]


def test_audio_arrives_while_the_model_is_still_generating():
    """Der erste Chunk liegt vor, bevor der zweite berechnet wird (kein
    Sammeln pro Satz)."""
    import faster_qwen3_tts

    engine = _loaded()
    voice = engine.prepare_voice(Voice(key="a", wav=wav_bytes(), transcript=None))
    stream = engine.synthesize("Ein ganz normaler Satz hier.", "de", voice, None)

    next(stream)
    assert len(_calls("generate")) == 1
    stream.close()  # Barge-in: der Rest wird nie erzeugt
    assert faster_qwen3_tts.CALLS[-1]["call"] == "generate"


def test_chunk_size_follows_the_setting(monkeypatch):
    monkeypatch.setattr(backend, "CHUNK_STEPS", 4)
    engine = _loaded()
    voice = engine.prepare_voice(Voice(key="a", wav=wav_bytes(), transcript=None))
    list(engine.synthesize("Ein ganz normaler Satz hier.", "de", voice, None))
    assert _calls("generate")[0]["chunk_size"] == 4


def test_unknown_or_unsupported_language_lets_qwen_detect_it(monkeypatch):
    engine = _loaded()
    voice = engine.prepare_voice(Voice(key="a", wav=wav_bytes(), transcript=None))
    list(engine.synthesize("Ein ganz normaler Satz hier.", "nl", voice, None))
    list(engine.synthesize("Ein ganz normaler Satz hier.", None, voice, None))
    monkeypatch.setenv("FAKE_QWEN_LANGUAGES", "auto,english")
    engine = _loaded()
    list(engine.synthesize("Ein ganz normaler Satz hier.", "de", voice, None))
    assert [call["language"] for call in _calls("generate")] == ["auto", "auto", "auto"]


def test_without_sample_there_is_no_voice():
    with pytest.raises(ValueError, match="Voice-Sample"):
        list(_loaded().synthesize("Hallo.", "de", None, None))


def test_nan_audio_points_to_float32_after_the_good_chunk():
    engine = _loaded()
    voice = engine.prepare_voice(Voice(key="a", wav=wav_bytes(), transcript=None))
    stream = engine.synthesize("NaN kommt heraus.", "de", voice, None)
    next(stream)  # der erste Chunk war in Ordnung
    with pytest.raises(RuntimeError, match="QWEN3_DTYPE=float32"):
        next(stream)


# ---- Ende zu Ende: HTTP-Vertrag + Modell-Unterprozess ---------------------------------------


def test_engine_app_clones_and_streams_through_the_kit(monkeypatch, tmp_path):
    """Wie im Container: create_app, Modell im Unterprozess (mit dem
    faster_qwen3_tts-Nachbau), Referenz per Multipart, Sprache als ISO-Code."""
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_QWEN_LOG", str(log))
    monkeypatch.setenv("FAKE_CUDA_CAPABILITY", "7.5")
    monkeypatch.setattr(backend, "MODEL", str(tmp_path))  # eigener Pfad: kein Download
    app = create_app("tts_qwen3_stream.backend:Qwen3StreamBackend", WorkerSettings(device="cuda:1"))

    with TestClient(app) as client:
        for _ in range(100):
            if client.get("/v1/device").json()["state"] == "idle":
                break
            time.sleep(0.05)
        info = client.get("/v1/info").json()
        assert info["name"] == "Qwen3-TTS Streaming" and info["streaming"] == "chunk"
        status = client.post("/v1/device", json={"device": "cuda:1"}).json()
        assert status["state"] == "ready" and status["effective"] == "cuda:1"
        response = client.post(
            "/v1/synthesize",
            data={"text": "Das ist der erste Satz. Und das hier ist der zweite Satz.",
                  "language": "de", "ref_text": "Mein Sample.", "load_timeout_s": "10"},
            files={"ref_audio": ("stimme.wav", wav_bytes(), "audio/wav")},
        )

    assert response.status_code == 200 and response.headers["x-sample-rate"] == "24000"
    pcm = np.frombuffer(response.content, dtype="<i2")
    assert len(pcm) == 4 * 2400 and np.abs(pcm).max() > 7000
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call["call"] for call in calls] == ["from_pretrained", "warmup", "prompt", "generate", "generate"]
    assert calls[0]["dtype"] == "torch.float16" and calls[0]["device"] == "cuda:1"
    assert calls[2]["ref_text"] == "Mein Sample." and calls[3]["language"] == "german"
