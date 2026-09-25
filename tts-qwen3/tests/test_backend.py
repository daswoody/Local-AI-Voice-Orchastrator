"""Qwen3-Backend gegen den qwen_tts-Nachbau: Download, dtype, Klon, Saetze."""

import json
import time

import numpy as np
import pytest
from conftest import wav_bytes
from fastapi.testclient import TestClient

from tts_engine_kit import Voice, create_app
from tts_engine_kit.worker import WorkerSettings
from tts_qwen3 import backend


def _calls(kind: str) -> list[dict]:
    import qwen_tts

    return [call for call in qwen_tts.CALLS if call["call"] == kind]


def _loaded(device: str = "cuda:0") -> backend.Qwen3Backend:
    engine = backend.Qwen3Backend(model_dir="/models/qwen3")
    engine.load(device)
    return engine


# ---- Steckbrief und Gewichte ------------------------------------------------------------


def test_info_describes_a_german_capable_clone_engine(monkeypatch):
    info = backend.Qwen3Backend.info()
    assert "de" in info.languages and info.sample_rate == 24000
    assert info.needs_sample and info.uses_transcript and info.streaming == "sentence"
    assert info.name == "Qwen3-TTS 1.7B" and info.vram_mb == 5000
    monkeypatch.setattr(backend, "MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
    assert backend.Qwen3Backend.info().vram_mb == 2500


def test_weights_are_downloaded_once_into_the_volume(monkeypatch, tmp_path):
    import huggingface_hub

    downloads = []

    def fake_download(repo_id, local_dir, token=None):
        downloads.append((repo_id, local_dir, token))

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_download)
    monkeypatch.setattr(backend, "MODELS_DIR", tmp_path)
    monkeypatch.setenv("HF_TOKEN", "hf_test")

    first = backend.Qwen3Backend.prepare()
    second = backend.Qwen3Backend.prepare()

    target = str(tmp_path / "Qwen--Qwen3-TTS-12Hz-1.7B-Base")
    assert first == second == {"model_dir": target}
    assert downloads == [("Qwen/Qwen3-TTS-12Hz-1.7B-Base", target, "hf_test")]


def test_own_weights_in_the_volume_are_used_as_they_are(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "MODEL", str(tmp_path))
    assert backend.Qwen3Backend.prepare() == {"model_dir": str(tmp_path)}


def test_invalid_dtype_is_rejected_before_anything_loads(monkeypatch):
    monkeypatch.setattr(backend, "DTYPE", "int4")
    with pytest.raises(ValueError, match="QWEN3_DTYPE"):
        backend.Qwen3Backend.prepare()


# ---- Laden ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("device", "capability", "expected"), [
    ("cpu", "7.5", "torch.float32"),
    ("cuda:1", "7.5", "torch.float16"),   # 2080 Ti: kein natives bfloat16
    ("cuda", "8.6", "torch.bfloat16"),
])
def test_dtype_follows_the_card(monkeypatch, device, capability, expected):
    monkeypatch.setenv("FAKE_CUDA_CAPABILITY", capability)
    _loaded(device)
    assert _calls("from_pretrained")[-1] == {"call": "from_pretrained", "path": "/models/qwen3",
                                              "device_map": device, "dtype": expected, "attn": "sdpa"}


def test_dtype_can_be_forced(monkeypatch):
    monkeypatch.setattr(backend, "DTYPE", "float32")
    _loaded("cuda:0")
    assert _calls("from_pretrained")[-1]["dtype"] == "torch.float32"


# ---- Klonen und Sprechen ------------------------------------------------------------------


def test_transcript_selects_the_closer_icl_clone():
    engine = _loaded()
    engine.prepare_voice(Voice(key="a", wav=wav_bytes(width=3, channels=2), transcript=" Mein Sample. "))
    engine.prepare_voice(Voice(key="b", wav=wav_bytes(), transcript=None))

    icl, xvec = _calls("prompt")
    assert icl["ref_text"] == "Mein Sample." and icl["x_vector_only_mode"] is False
    assert icl["rate"] == 24000 and icl["samples"] == 12000 and icl["peak"] == pytest.approx(0.5, abs=0.01)
    assert xvec["ref_text"] is None and xvec["x_vector_only_mode"] is True


def test_speaks_sentence_by_sentence_in_the_requested_language():
    engine = _loaded()
    voice = engine.prepare_voice(Voice(key="a", wav=wav_bytes(), transcript="Mein Sample."))

    chunks = list(engine.synthesize("Guten Morgen, wie geht es dir? Heute scheint die Sonne.",
                                    "de", voice, None))

    assert [rate for rate, _ in chunks] == [24000, 24000]
    assert all(len(pcm) == 2400 * 2 for _, pcm in chunks)
    calls = _calls("generate")
    assert [call["text"] for call in calls] == ["Guten Morgen, wie geht es dir?",
                                                "Heute scheint die Sonne."]
    assert {call["language"] for call in calls} == {"german"}
    assert calls[0]["prompt"] == [{"prompt": "Mein Sample."}]


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


def test_nan_audio_points_to_float32():
    engine = _loaded()
    voice = engine.prepare_voice(Voice(key="a", wav=wav_bytes(), transcript=None))
    with pytest.raises(RuntimeError, match="QWEN3_DTYPE=float32"):
        list(engine.synthesize("NaN kommt heraus.", "de", voice, None))


# ---- Ende zu Ende: HTTP-Vertrag + Modell-Unterprozess ---------------------------------------


def test_engine_app_clones_and_speaks_through_the_kit(monkeypatch, tmp_path):
    """Wie im Container: create_app, Modell im Unterprozess (mit dem
    qwen_tts-Nachbau), Referenz per Multipart, Sprache als ISO-Code."""
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_QWEN_LOG", str(log))
    monkeypatch.setenv("FAKE_CUDA_CAPABILITY", "7.5")
    monkeypatch.setenv("QWEN3_MODEL", str(tmp_path))  # eigener Pfad: kein Download
    monkeypatch.setattr(backend, "MODEL", str(tmp_path))
    app = create_app("tts_qwen3.backend:Qwen3Backend", WorkerSettings(device="cuda:1"))

    with TestClient(app) as client:
        for _ in range(100):
            if client.get("/v1/device").json()["state"] == "idle":
                break
            time.sleep(0.05)
        assert client.get("/v1/info").json()["name"] == "Qwen3-TTS"  # eigener Pfad: ohne Groesse
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
    assert len(pcm) == 2 * 2400 and np.abs(pcm).max() > 7000
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call["call"] for call in calls] == ["from_pretrained", "prompt", "generate", "generate"]
    assert calls[0]["dtype"] == "torch.float16" and calls[0]["device_map"] == "cuda:1"
    assert calls[1]["ref_text"] == "Mein Sample." and calls[2]["language"] == "german"
