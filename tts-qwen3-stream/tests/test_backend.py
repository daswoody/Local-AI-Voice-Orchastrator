"""Streaming-Backend gegen den qwentts_cpp-Nachbau: Download, Quantisierung,
Karte, Klon, Chunks."""

import json
import os
import time

import numpy as np
import pytest
from conftest import wav_bytes
from fastapi.testclient import TestClient

from tts_engine_kit import Voice, create_app
from tts_engine_kit.worker import WorkerSettings
from tts_qwen3_stream import backend


def _calls(kind: str) -> list[dict]:
    import qwentts_cpp

    return [call for call in qwentts_cpp.CALLS if call["call"] == kind]


def _loaded(device: str = "cuda:0") -> backend.Qwen3StreamBackend:
    engine = backend.Qwen3StreamBackend(talker_path="/models/talker.gguf", codec_path="/models/codec.gguf")
    engine.load(device)
    return engine


def _voice(engine, transcript="Mein Sample.", **wav):
    return engine.prepare_voice(Voice(key="a", wav=wav_bytes(**wav), transcript=transcript))


# ---- Steckbrief und Gewichte ------------------------------------------------------------


def test_info_describes_a_streaming_german_capable_clone_engine(monkeypatch):
    info = backend.Qwen3StreamBackend.info()
    assert "de" in info.languages and info.sample_rate == 24000
    assert info.needs_sample and info.uses_transcript and info.streaming == "chunk"
    assert info.name == "Qwen3-TTS 0.6B Q8_0 Streaming" and info.vram_mb == 750 + 800
    monkeypatch.setattr(backend, "QUANT", "q4")
    assert backend.Qwen3StreamBackend.info().name == "Qwen3-TTS 0.6B Q4_K_M Streaming"
    monkeypatch.setattr(backend, "MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
    assert backend.Qwen3StreamBackend.info().vram_mb == 1350 + 800


@pytest.mark.parametrize(("quant", "codec_quant", "talker", "codec"), [
    ("Q8_0", "", "qwen-talker-0.6b-base-Q8_0.gguf", "qwen-tokenizer-12hz-Q8_0.gguf"),
    ("q4", "", "qwen-talker-0.6b-base-Q4_K_M.gguf", "qwen-tokenizer-12hz-Q4_K_M.gguf"),
    ("Q4_K_M", "Q8", "qwen-talker-0.6b-base-Q4_K_M.gguf", "qwen-tokenizer-12hz-Q8_0.gguf"),
    ("fp32", "", "qwen-talker-0.6b-base-F32.gguf", "qwen-tokenizer-12hz-F32.gguf"),
    ("BF16", "", "qwen-talker-0.6b-base-BF16.gguf", "qwen-tokenizer-12hz-BF16.gguf"),
])
def test_quantisation_selects_the_gguf_files(quant, codec_quant, talker, codec):
    assert backend.gguf_names("Qwen/Qwen3-TTS-12Hz-0.6B-Base", quant, codec_quant) == (talker, codec)


def test_unknown_quantisation_or_model_is_rejected():
    with pytest.raises(ValueError, match="Q8_0"):
        backend.normalize_quant("Q2")
    with pytest.raises(ValueError, match="QWEN3_MODEL"):
        backend.gguf_names("irgendein/Modell", "Q8_0")


def test_gguf_files_are_downloaded_once_into_the_volume(monkeypatch, tmp_path):
    import huggingface_hub

    downloads = []

    def fake_download(repo_id, filename, cache_dir=None, token=None):
        downloads.append((repo_id, filename, cache_dir, token))
        return str(tmp_path / filename)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    monkeypatch.setattr(backend, "MODELS_DIR", tmp_path)
    monkeypatch.setenv("HF_TOKEN", "hf_test")

    paths = backend.Qwen3StreamBackend.prepare()

    assert paths == {"talker_path": str(tmp_path / "qwen-talker-0.6b-base-Q8_0.gguf"),
                     "codec_path": str(tmp_path / "qwen-tokenizer-12hz-Q8_0.gguf")}
    cache = str(tmp_path / "gguf")
    assert downloads == [
        ("Serveurperso/Qwen3-TTS-GGUF", "qwen-talker-0.6b-base-Q8_0.gguf", cache, "hf_test"),
        ("Serveurperso/Qwen3-TTS-GGUF", "qwen-tokenizer-12hz-Q8_0.gguf", cache, "hf_test")]


def test_own_gguf_files_in_the_volume_are_used_as_they_are(monkeypatch, tmp_path):
    talker, codec = tmp_path / "t.gguf", tmp_path / "c.gguf"
    talker.touch()
    codec.touch()
    monkeypatch.setattr(backend, "TALKER_GGUF", str(talker))
    monkeypatch.setattr(backend, "CODEC_GGUF", str(codec))
    assert backend.Qwen3StreamBackend.prepare() == {"talker_path": str(talker), "codec_path": str(codec)}

    monkeypatch.setattr(backend, "CODEC_GGUF", "")
    with pytest.raises(ValueError, match="zusammen"):
        backend.Qwen3StreamBackend.prepare()
    monkeypatch.setattr(backend, "CODEC_GGUF", str(tmp_path / "fehlt.gguf"))
    with pytest.raises(ValueError, match="nicht gefunden"):
        backend.Qwen3StreamBackend.prepare()


@pytest.mark.parametrize(("name", "value", "message"), [
    ("CHUNK_STEPS", 0, "QWEN3_CHUNK_STEPS"),
    ("FIRST_CHUNK_STEPS", 3, "QWEN3_FIRST_CHUNK_STEPS"),
    ("QUANT", "Q2", "Q8_0"),
])
def test_invalid_settings_are_rejected_before_anything_loads(monkeypatch, name, value, message):
    monkeypatch.setattr(backend, name, value)
    with pytest.raises(ValueError, match=message):
        backend.Qwen3StreamBackend.prepare()


# ---- Laden ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("device", "visible"), [("cuda:1", "1"), ("cuda", "0"), ("cuda:0", "0"), ("cpu", "")])
def test_the_assigned_card_becomes_the_only_visible_one(device, visible):
    _loaded(device)
    assert _calls("init")[-1]["cuda_visible"] == visible
    assert os.environ["CUDA_VISIBLE_DEVICES"] == visible


def test_loads_the_gguf_files_with_flash_attention():
    _loaded()
    call = _calls("init")[-1]
    assert call["talker"] == "/models/talker.gguf" and call["codec"] == "/models/codec.gguf"
    assert call["use_fa"] is True and call["clamp_fp16"] is False and call["log_level"] == "info"


def test_a_model_that_is_not_the_base_model_is_refused(monkeypatch):
    monkeypatch.setenv("FAKE_QWEN_MODEL_TYPE", "custom_voice")
    engine = backend.Qwen3StreamBackend(talker_path="t", codec_path="c")
    with pytest.raises(RuntimeError, match="Base.*custom_voice"):
        engine.load("cuda:0")


# ---- Klonen und Sprechen ------------------------------------------------------------------


def test_transcript_selects_the_closer_icl_clone_and_ends_with_silence():
    engine = _loaded()
    icl = _voice(engine, " Mein Sample. ", width=3, channels=2)
    xvec = _voice(engine, None)

    first, second = _calls("extract")
    # 0,5 s Sample + 0,5 s Stille; die Stille verhindert, dass der letzte Laut des Samples
    # in den Anfang der Sprache blutet
    assert first["samples"] == 12000 + 12000 and first["dtype"] == "float32"
    assert first["peak"] == pytest.approx(0.5, abs=0.01) and first["tail_peak"] == 0.0
    # ohne Transkript nur der Stimm-Fingerabdruck - dort bleibt das Sample, wie es ist
    assert second["samples"] == 12000
    assert icl["text"] == "Mein Sample." and icl["codes"] is not None
    assert xvec["text"] is None and xvec["codes"] is None


def test_other_sample_rates_are_resampled_to_24khz():
    _voice(_loaded(), None, rate=16000, seconds=0.5)
    assert _calls("extract")[0]["samples"] == 12000


def test_silence_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(backend, "REF_SILENCE_S", 0)
    _voice(_loaded())
    assert _calls("extract")[0]["samples"] == 12000


def test_streams_chunks_sentence_by_sentence_in_the_requested_language():
    engine = _loaded()
    voice = _voice(engine)

    chunks = list(engine.synthesize("Guten Morgen, wie geht es dir? Heute scheint die Sonne.",
                                    "de", voice, None))

    assert [rate for rate, _ in chunks] == [24000] * 4  # 2 Saetze x 2 Chunks
    assert all(len(pcm) == 2400 * 2 for _, pcm in chunks)
    calls = _calls("stream")
    assert [call["text"] for call in calls] == ["Guten Morgen, wie geht es dir?",
                                                "Heute scheint die Sonne."]
    assert {call["lang"] for call in calls} == {"german"}
    assert {call["ref_text"] for call in calls} == {"Mein Sample."} and all(call["icl"] for call in calls)
    # 8 Frames a 80 ms = 0,64 s; der erste Chunk 4 Frames
    assert {call["codec_chunk_sec"] for call in calls} == {0.64}
    assert {call["first_chunk_frames"] for call in calls} == {4}


def test_without_transcript_only_the_voice_fingerprint_is_sent():
    engine = _loaded()
    list(engine.synthesize("Ein ganz normaler Satz hier.", "de", _voice(engine, None), None))
    call = _calls("stream")[0]
    assert call["has_spk"] is True and call["icl"] is False and call["ref_text"] is None


def test_audio_arrives_while_the_model_is_still_generating():
    """Der erste Chunk liegt vor, bevor der zweite berechnet wird (kein
    Sammeln pro Satz)."""
    engine = _loaded()
    stream = engine.synthesize("Ein ganz normaler Satz hier.", "de", _voice(engine), None)
    next(stream)
    stream.close()  # Barge-in: der Rest wird nie erzeugt
    assert len(_calls("stream")) == 1


def test_chunk_sizes_follow_the_settings(monkeypatch):
    monkeypatch.setattr(backend, "CHUNK_STEPS", 5)
    monkeypatch.setattr(backend, "FIRST_CHUNK_STEPS", 2)
    engine = _loaded()
    list(engine.synthesize("Ein ganz normaler Satz hier.", "de", _voice(engine), None))
    call = _calls("stream")[0]
    assert call["codec_chunk_sec"] == pytest.approx(0.4) and call["first_chunk_frames"] == 2


def test_unknown_or_unsupported_language_lets_qwen_detect_it(monkeypatch):
    engine = _loaded()
    voice = _voice(engine)
    list(engine.synthesize("Ein ganz normaler Satz hier.", "nl", voice, None))
    list(engine.synthesize("Ein ganz normaler Satz hier.", None, voice, None))
    monkeypatch.setenv("FAKE_QWEN_LANGUAGES", "english")
    engine = _loaded()
    list(engine.synthesize("Ein ganz normaler Satz hier.", "de", voice, None))
    assert [call["lang"] for call in _calls("stream")] == ["auto", "auto", "auto"]


def test_without_sample_there_is_no_voice():
    with pytest.raises(ValueError, match="Voice-Sample"):
        list(_loaded().synthesize("Hallo.", "de", None, None))


def test_nan_audio_is_refused_after_the_good_chunk():
    engine = _loaded()
    stream = engine.synthesize("NaN kommt heraus.", "de", _voice(engine), None)
    next(stream)  # der erste Chunk war in Ordnung
    with pytest.raises(RuntimeError, match="NaN"):
        next(stream)


# ---- Ende zu Ende: HTTP-Vertrag + Modell-Unterprozess ---------------------------------------


def test_engine_app_clones_and_streams_through_the_kit(monkeypatch, tmp_path):
    """Wie im Container: create_app, Modell im Unterprozess (mit dem
    qwentts_cpp-Nachbau), Referenz per Multipart, Sprache als ISO-Code."""
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_QWEN_LOG", str(log))
    talker, codec = tmp_path / "t.gguf", tmp_path / "c.gguf"
    talker.touch()
    codec.touch()
    monkeypatch.setattr(backend, "TALKER_GGUF", str(talker))  # eigene Dateien: kein Download
    monkeypatch.setattr(backend, "CODEC_GGUF", str(codec))
    app = create_app("tts_qwen3_stream.backend:Qwen3StreamBackend", WorkerSettings(device="cuda:1"))

    with TestClient(app) as client:
        for _ in range(100):
            if client.get("/v1/device").json()["state"] == "idle":
                break
            time.sleep(0.05)
        info = client.get("/v1/info").json()
        assert info["name"] == "Qwen3-TTS 0.6B Q8_0 Streaming" and info["streaming"] == "chunk"
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
    assert [call["call"] for call in calls] == ["init", "extract", "stream", "stream"]
    assert calls[0]["cuda_visible"] == "1"  # "cuda:1" aus dem Panel
    assert calls[2]["ref_text"] == "Mein Sample." and calls[2]["lang"] == "german"
