"""Abgleich mit dem ECHTEN qwentts-cpp-python-Paket (nur wenn installiert, z. B.
im Image bzw. mit `uv sync --extra engine`): Die Aufrufe des Backends und der
Nachbau in tests/fakes muessen zu dessen API passen. Bibliothek (libqwen),
Karte und Gewichte braucht das nicht - die gibt es nur im Container."""

import inspect

import pytest

from tts_qwen3_stream import backend

pytestmark = pytest.mark.real_qwentts
qwentts_cpp = pytest.importorskip("qwentts_cpp", reason="echtes qwentts-cpp-python nicht installiert")
if not hasattr(qwentts_cpp, "__file__") or "fakes" in str(qwentts_cpp.__file__):
    pytest.skip("nur der Nachbau gefunden", allow_module_level=True)


def test_backend_calls_use_real_parameter_names():
    init = inspect.signature(qwentts_cpp.QwenTTS.__init__).parameters
    stream = inspect.signature(qwentts_cpp.QwenTTS.stream).parameters
    assert {"talker_path", "codec_path", "use_fa", "clamp_fp16", "log_level"} <= set(init)
    assert {"text", "lang", "ref_spk_emb", "ref_codes", "ref_text", "codec_chunk_sec",
            "first_chunk_frames"} <= set(stream)
    # Sampling-Werte aus dem Steckbrief (v1.28) gehen 1:1 als Keywords durch.
    assert set(backend.Qwen3StreamBackend.info().sampling) <= set(stream)
    for method in ("extract_voice_ref", "language_names", "model_type"):
        assert callable(getattr(qwentts_cpp.QwenTTS, method))


def test_voice_ref_has_the_fields_the_backend_reads():
    import numpy as np

    ref = qwentts_cpp.VoiceRef(ref_spk_emb=np.ones(4, dtype=np.float32), ref_codes=np.ones((2, 3), dtype=np.int32))
    assert ref.ref_spk_emb.shape == (4,) and ref.ref_codes.shape == (2, 3)


def test_codec_frames_are_80_ms():
    """CHUNK_STEPS / 12.5 -> Frames: das Paket rechnet codec_chunk_sec * 12.5."""
    source = inspect.getsource(qwentts_cpp.QwenTTS.stream)
    assert "codec_chunk_sec * 12.5" in source


@pytest.mark.parametrize(("model", "quant", "codec_quant"), [
    ("Qwen/Qwen3-TTS-12Hz-0.6B-Base", "Q8_0", ""),
    ("Qwen/Qwen3-TTS-12Hz-0.6B-Base", "Q4", ""),
    ("Qwen/Qwen3-TTS-12Hz-1.7B-Base", "BF16", ""),
    ("Qwen/Qwen3-TTS-12Hz-1.7B-Base", "fp32", ""),
    ("Qwen/Qwen3-TTS-12Hz-0.6B-Base", "Q4_K_M", "Q8"),
])
def test_gguf_file_names_match_the_real_package(monkeypatch, model, quant, codec_quant):
    import huggingface_hub
    from qwentts_cpp.models import GGUF_REPO, resolve_gguf_paths

    asked = []
    monkeypatch.setattr(huggingface_hub, "hf_hub_download",
                        lambda filename, **kwargs: asked.append(filename) or f"/x/{filename}")
    resolve_gguf_paths(model, quant=quant)
    talker, codec = backend.gguf_names(model, quant)
    assert asked == [talker, codec] and backend.GGUF_REPO == GGUF_REPO

    if codec_quant:  # getrennte Codec-Stufe: Talker wie QUANT, Codec wie CODEC_QUANT
        asked.clear()
        resolve_gguf_paths(model, quant=codec_quant)
        assert backend.gguf_names(model, quant, codec_quant)[1] == asked[1]
